"""Tests for frame decoding and battery operations, using a frame read from a real BL1850 pack."""

import json
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

    def __init__(
        self,
        frame: bytes = GOOD,
        cells: list[float] | None = None,
        after_reset: bytes | None = None,
    ) -> None:
        """Set the state of the fake battery.

        :param frame: Stored frame.
        :param cells: Cell voltages.
        :param after_reset: Stored frame after the first error reset.
        """
        self.frame = frame
        self.cells = HEALTHY_CELLS if cells is None else cells
        self.after_reset = after_reset
        self.log: list[bytes] = []
        self.pending = b''
        self.written: list[bytes] = []
        self.refuse_test_mode = False
        self.refuse_exit = False
        self.refuse_arm = False
        self.ignore_store = False
        self.echo_after_write = ROM

    def query(self, command: int, data: bytes = b'', reply_length: int = 0) -> bytes:
        """Answer a read ROM transaction and apply frame writes.

        :param command: Firmware command.
        :param data: Data bytes.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        if data == b'\xaa\x00':
            return ROM + self.frame

        self.log.append(data)
        if data[:2] == b'\x0f\x00':
            self.pending = data[2:]

            return self.echo_after_write

        if data == b'\x55\xa5' and not self.ignore_store:
            self.frame = self.pending
            self.written.append(self.pending)

        if data == b'\xda\x04' and self.after_reset is not None:
            self.frame = self.after_reset
            self.after_reset = None

        return ROM + b'\x07' * (reply_length - len(ROM))

    def skip(self, data: bytes, reply_length: int = 0) -> bytes:
        """Answer a skip ROM transaction.

        :param data: Bytes written.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        self.log.append(data)
        if data == bytes.fromhex('d996a5'):
            return b'\xff' if self.refuse_test_mode else b'\x06'

        if data == bytes.fromhex('d9ffff'):
            return b'\xff' if self.refuse_test_mode or self.refuse_exit else b'\x06'

        if data == b'\xf0\x00':
            return b'\xff' * reply_length if self.refuse_arm else self.frame

        if data == bytes.fromhex('d42c0002'):
            return b'\x01\x02\x06'

        if data == bytes.fromhex('d700000c'):
            values = [round(sum(self.cells) * 1000), *[round(cell * 1000) for cell in self.cells]]

            return b''.join(value.to_bytes(2, 'little') for value in values) + b'\x06'

        if data == bytes.fromhex('d70e0002'):
            return (3004).to_bytes(2, 'little') + b'\x06'

        return b'\xff' * reply_length

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


def steps(link: FakeLink) -> list[str]:
    """List the write related commands that were sent.

    :param link: Fake link.
    :return: First two bytes of each command, in hex.
    """
    known = ('d996', 'd9ff', 'da04', 'f000', '0f00', '55a5')

    return [entry[:2].hex() for entry in link.log if entry[:2].hex() in known]


def test_unlock_writes_in_order_and_backs_up(tmp_path: Path) -> None:
    """Check the write sequence and the backup.

    :param tmp_path: Temporary folder.
    """
    link = FakeLink(frame=locked_frame())
    result = Battery(link=link).unlock()
    assert result.lock_causes == []
    reset = ['d996', 'da04', 'd9ff']
    assert steps(link=link) == [*reset, 'd996', 'f000', '0f00', '55a5', 'd9ff', *reset]
    assert len(list(tmp_path.glob('*.json'))) == 1


def test_unlock_skips_write_when_reset_is_enough() -> None:
    """Check that the frame is not written when the error reset clears the lock."""
    link = FakeLink(frame=locked_frame(), after_reset=GOOD)
    assert Battery(link=link).unlock().data == GOOD
    assert '0f00' not in steps(link=link)


def test_unlock_builds_frame_from_fresh_read() -> None:
    """Check that a change made by the error reset is kept in the written frame."""
    changed = bytearray(locked_frame())
    changed[30] = 0x11
    link = FakeLink(frame=locked_frame(), after_reset=bytes(changed))
    Battery(link=link).unlock()
    assert link.written[0][30] == 0x11


def test_unlock_stops_when_test_mode_is_refused() -> None:
    """Check that nothing is written when test mode is not acknowledged."""
    link = FakeLink(frame=locked_frame())
    link.refuse_test_mode = True
    with pytest.raises(BatteryError, match='test mode'):
        Battery(link=link).unlock()

    assert not {'da04', 'f000', '0f00', '55a5'} & set(steps(link=link))


def test_unlock_stops_when_arm_is_refused() -> None:
    """Check that nothing is written when the battery does not arm."""
    link = FakeLink(frame=locked_frame())
    link.refuse_arm = True
    with pytest.raises(BatteryError, match='reinsert'):
        Battery(link=link).unlock()

    assert '0f00' not in steps(link=link)
    assert steps(link=link)[-1] == 'd9ff'


def test_unlock_skips_store_on_wrong_rom_echo() -> None:
    """Check that the store command is not sent when another ROM id answers."""
    link = FakeLink(frame=locked_frame())
    link.echo_after_write = b'\xff' * 8
    with pytest.raises(BatteryError, match='ROM id changed'):
        Battery(link=link).unlock()

    assert '55a5' not in steps(link=link)
    assert steps(link=link)[-1] == 'd9ff'


def test_unlock_reports_pack_that_stays_locked() -> None:
    """Check that a write the battery did not keep is an error."""
    link = FakeLink(frame=locked_frame())
    link.ignore_store = True
    with pytest.raises(BatteryError, match='still locked'):
        Battery(link=link).unlock()


def test_missing_exit_acknowledge_is_an_error() -> None:
    """Check that a pack that may still be in test mode is reported."""
    link = FakeLink()
    link.refuse_exit = True
    with pytest.raises(BatteryError, match='exit-test-mode'):
        Battery(link=link).reset_errors()


def write_backup(path: Path, frame: bytes, rom: bytes = ROM) -> Path:
    """Write a backup file.

    :param path: Folder for the file.
    :param frame: Frame to store.
    :param rom: ROM id to store.
    :return: Backup path.
    """
    backup = path / 'saved.backup'
    backup.write_text(json.dumps({'rom': rom.hex(), 'frame': frame.hex()}))

    return backup


def test_restore_undoes_an_unlock(tmp_path: Path) -> None:
    """Check that a frame differing only in the lock fields is written back.

    :param tmp_path: Temporary folder.
    """
    saved = Frame(data=locked_frame()).repaired().data
    saved = bytes([saved[0], *saved[1:17], saved[17] | 0x01, *saved[18:]])
    link = FakeLink()
    assert Battery(link=link).restore(path=write_backup(path=tmp_path, frame=saved)).data == saved


def test_restore_refuses_changed_history(tmp_path: Path) -> None:
    """Check that an old backup cannot rewrite the cycle count.

    :param tmp_path: Temporary folder.
    """
    data = bytearray(GOOD)
    data[27] = 0
    link = FakeLink()
    with pytest.raises(BatteryError, match='beyond the lock fields'):
        Battery(link=link).restore(path=write_backup(path=tmp_path, frame=bytes(data)))

    assert '0f00' not in steps(link=link)


def test_restore_refuses_another_battery(tmp_path: Path) -> None:
    """Check that a backup of another pack is refused.

    :param tmp_path: Temporary folder.
    """
    with pytest.raises(BatteryError, match='another battery'):
        Battery(link=FakeLink()).restore(path=write_backup(path=tmp_path, frame=GOOD, rom=b'\x01' * 8))


def test_restore_refuses_broken_file(tmp_path: Path) -> None:
    """Check that a broken backup gives a clean error.

    :param tmp_path: Temporary folder.
    """
    backup = tmp_path / 'broken.backup'
    backup.write_text('{"rom": "00"}')
    with pytest.raises(BatteryError, match='cannot use backup'):
        Battery(link=FakeLink()).restore(path=backup)


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


class TypeFiveLink(FakeLink):
    """Answers like a real F0513 pack, including the scrambled replies after F0 00."""

    def __init__(self) -> None:
        """Start with clean replies."""
        super().__init__()
        self.scrambled = False

    def query(self, command: int, data: bytes = b'', reply_length: int = 0) -> bytes:
        """Answer with a ROM id that marks a type 5 pack.

        :param command: Firmware command.
        :param data: Data bytes.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        return bytes.fromhex('1103010252CA0737') + self.frame

    def skip(self, data: bytes, reply_length: int = 0) -> bytes:
        """Answer a skip ROM transaction.

        :param data: Bytes written.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        self.log.append(data)
        if data == b'\xf0\x00':
            self.scrambled = True

            return b''

        if self.scrambled:
            return b'\xb0\x28'

        replies = {0x31: '2b10', 0x32: '2910', 0x33: '2810', 0x34: '2910', 0x35: '2910', 0x52: 'b50b'}

        return bytes.fromhex(replies[data[0]]) if data[0] in replies else b'\xff' * reply_length


def test_type_five_read_sends_no_clear() -> None:
    """Check the raw replies of a real BL1860B and that the state changing clear is not sent."""
    link = TypeFiveLink()
    report = Battery(link=link).report()
    assert report.controller_type == 5
    assert report.live.cell_voltages == [4.139, 4.137, 4.136, 4.137, 4.137]
    assert report.live.temperature_c == 26.55
    assert b'\xf0\x00' not in link.log


def test_type_five_rejects_implausible_cells() -> None:
    """Check that scrambled replies are not shown as measurements."""
    link = TypeFiveLink()
    link.scrambled = True
    assert Battery(link=link).report().live.cell_voltages == []
