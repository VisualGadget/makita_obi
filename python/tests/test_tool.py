"""Tests for frame decoding and battery operations, using a frame read from a real BL1850 pack."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from makita_tool import battery as battery_module
from makita_tool.battery import Battery, BatteryError, LiveData, kelvin_tenths_to_c
from makita_tool.frame import Frame, manufacture_date, set_nybble

ROM = bytes.fromhex('0E0A0F6708070E24')
GOOD = bytes.fromhex('F136B6C318580000949440210180024B43D08E1BF06B004302020E32002200B7')
HEALTHY_CELLS = [4.11, 4.114, 4.116, 4.109, 4.117]


def locked_frame() -> bytes:
    """Build a frame with the charger lock set.

    :return: Frame bytes.
    """
    data = bytearray(GOOD)
    set_nybble(data=data, index=34, value=1)

    return bytes(data)


class FakeLink:
    """Records transactions and answers like a type 3 battery."""

    def __init__(self, frame: bytes = GOOD, cells: list[float] | None = None) -> None:
        """Set the state of the fake battery.

        :param frame: Stored frame.
        :param cells: Cell voltages.
        """
        self.frame = frame
        self.cells = HEALTHY_CELLS if cells is None else cells
        self.log: list[bytes] = []

    def query(self, command: int, data: bytes = b'', reply_length: int = 0) -> bytes:
        """Answer the read ROM frame.

        :param command: Firmware command.
        :param data: Data bytes.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        return ROM + self.frame

    def skip(self, data: bytes, reply_length: int = 0) -> bytes:
        """Answer a skip ROM transaction.

        :param data: Bytes written.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        self.log.append(data)
        if data == bytes.fromhex('d42c0002'):
            return b'\x01\x02\x06'

        if data == bytes.fromhex('d700000c'):
            values = [round(sum(self.cells) * 1000), *[round(cell * 1000) for cell in self.cells]]

            return b''.join(value.to_bytes(2, 'little') for value in values) + b'\x06'

        if data == bytes.fromhex('d70e0002'):
            return (3004).to_bytes(2, 'little') + b'\x06'

        return b'\xff' * reply_length

    def rom(self, data: bytes, reply_length: int = 0) -> bytes:
        """Answer a read ROM transaction and apply a frame write.

        :param data: Bytes written.
        :param reply_length: Number of bytes read.
        :return: Reply bytes.
        """
        self.log.append(data)
        if data[:2] == b'\x0f\x00':
            self.frame = data[2:]

        return b'\x06' * reply_length

    def raw(self, data: bytes, reply_length: int = 0) -> bytes:
        """Answer a raw transaction.

        :param data: Bytes written.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        return b'\xff' * reply_length

    @contextmanager
    def session(self) -> Iterator[None]:
        """Do nothing, like a held Enable line."""
        yield


@pytest.fixture(autouse=True)
def fast_and_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove waits and keep backups out of the home folder.

    :param monkeypatch: Pytest patcher.
    :param tmp_path: Temporary folder.
    """
    monkeypatch.setattr(battery_module.time, 'sleep', lambda _: None)
    monkeypatch.setattr(battery_module, 'BACKUP_DIR', tmp_path)


def test_real_frame_decodes() -> None:
    """Check the fields of the frame read from the real pack."""
    frame = Frame(data=GOOD)
    assert frame.battery_type == 18
    assert frame.capacity == 52
    assert frame.cycle_count == 35
    assert frame.failure_name == 'OK'
    assert frame.overdischarge == 32
    assert frame.overload == 32
    assert frame.lock_causes == []
    assert frame.likely_model == 'BL1850 or BL1850B'
    assert manufacture_date(rom=ROM) == '2014-10-15'


def test_temperature_uses_tenths_of_kelvin() -> None:
    """Check the temperature conversion."""
    assert kelvin_tenths_to_c(raw=3004) == 27.25


def test_repair_clears_lock_and_keeps_failure_code() -> None:
    """Check that a repair clears the lock without hiding a fault."""
    data = bytearray(locked_frame())
    set_nybble(data=data, index=40, value=0xF)
    repaired = Frame(data=bytes(data)).repaired()
    assert repaired.lock_causes == []
    assert repaired.failure_code == 0xF
    assert repaired.cycle_count == 35


def test_report_is_read_only() -> None:
    """Check that a report sends no test mode or write commands."""
    link = FakeLink()
    report = Battery(link=link).report()
    assert report.controller_type == 3
    assert report.live.temperature_c == 27.25
    assert all(entry[:1] not in (b'\xd9', b'\xf0', b'\x0f', b'\x55') for entry in link.log)


def test_unlock_does_nothing_on_healthy_pack() -> None:
    """Check that an unlocked pack is not written."""
    link = FakeLink()
    assert Battery(link=link).unlock().data == GOOD
    assert all(entry[:1] != b'\x0f' for entry in link.log)


def test_unlock_writes_in_order_and_backs_up(tmp_path: Path) -> None:
    """Check the write sequence and the backup.

    :param tmp_path: Temporary folder.
    """
    link = FakeLink(frame=locked_frame())
    result = Battery(link=link).unlock()
    assert result.lock_causes == []
    sequence = [entry[:2].hex() for entry in link.log]
    order = [sequence.index(step) for step in ('d996', 'f000', '0f00', '55a5', 'd9ff')]
    assert order == sorted(order)
    assert len(list(tmp_path.glob('*.json'))) == 1


@pytest.mark.parametrize(
    ('cells', 'reason'),
    [
        ([4.1, 4.1, 2.5, 4.1, 4.1], 'lowest cell'),
        ([4.1, 4.1, 3.5, 4.1, 4.1], 'imbalance'),
    ],
)
def test_unlock_refuses_bad_cells(cells: list[float], reason: str) -> None:
    """Check that a pack with bad cells is left alone.

    :param cells: Cell voltages.
    :param reason: Expected text in the error.
    """
    link = FakeLink(frame=locked_frame(), cells=cells)
    with pytest.raises(BatteryError, match=reason):
        Battery(link=link).unlock()

    assert all(entry[:1] != b'\x0f' for entry in link.log)


def test_unlock_refuses_dead_pack() -> None:
    """Check that a pack marked dead is refused."""
    data = bytearray(locked_frame())
    set_nybble(data=data, index=40, value=0xF)
    battery = Battery(link=FakeLink())
    blockers = battery.unlock_blockers(
        frame=Frame(data=bytes(data)),
        controller_type=3,
        live=LiveData(cell_voltages=HEALTHY_CELLS, temperature_c=25.0),
    )
    assert 'the battery reports itself as dead' in blockers


class TypeTwoLink(FakeLink):
    """Answers like a type 2 battery, whose counters only work in test mode."""

    def __init__(self) -> None:
        """Start outside test mode."""
        super().__init__()
        self.in_test_mode = False

    def skip(self, data: bytes, reply_length: int = 0) -> bytes:
        """Answer a skip ROM transaction.

        :param data: Bytes written.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        self.log.append(data)
        if data[:1] == b'\xd9':
            self.in_test_mode = data[1] == 0x96

            return b'\x06'

        replies = {
            bytes.fromhex('dc0a'): b'\x00' * 16 + b'\x06',
            bytes.fromhex('d6040502'): bytes.fromhex('eb0f06'),
            bytes.fromhex('d68d0501'): bytes.fromhex('0006'),
            bytes.fromhex('d65f0507'): bytes.fromhex('0019000000000006'),
        }
        if self.in_test_mode and data in replies:
            return replies[data]

        if data == bytes.fromhex('d42c0002'):
            return b'\xff' * reply_length

        return super().skip(data=data, reply_length=reply_length)


def test_type_two_counters_are_read_in_test_mode() -> None:
    """Check the counters of a type 2 pack and that test mode is left afterwards."""
    link = TypeTwoLink()
    report = Battery(link=link).report(probe_test_mode=True)
    assert report.controller_type == 2
    assert report.health_raw == 4075
    assert report.overdischarge_events == 0
    assert report.overload_events == 0
    assert link.in_test_mode is False


def test_type_two_stays_out_of_test_mode_without_probe() -> None:
    """Check that a plain report never enters test mode."""
    link = TypeTwoLink()
    report = Battery(link=link).report()
    assert report.controller_type is None
    assert all(entry[:1] != b'\xd9' for entry in link.log)
