"""Tests for frame decoding and battery operations, using a frame read from a real BL1850 pack."""

import json
from argparse import Namespace
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from makita_tool import __main__ as cli
from makita_tool import battery as battery_module
from makita_tool.battery import Battery, BatteryError, LiveData, NoRomIdError, StreamingPackError, kelvin_tenths_to_c
from makita_tool.frame import Frame, manufacture_date, set_nybble

ROM = bytes.fromhex('0E0A0F6708070E24')
GOOD = bytes.fromhex('F136B6C318580000949440210180024B43D08E1BF06B004302020E32002200B7')
TYPE5_ROM = bytes.fromhex('1103010252CA0737')
BL1860B = bytes.fromhex('F126BD131458000094944021D080020BC3D08E6760F3002302020E61004100E5')
TPCELL = bytes.fromhex('F136B6C318580000949440210180020943D08E1BF0650000000000000000000000')
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
        self.stuck_in_test_mode = False
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

        if data == bytes.fromhex('d6040502'):
            return (b'\x01\x02' if self.stuck_in_test_mode else b'\xff\xff')[:reply_length]

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
        if data != b'\xaa\x00':
            self.log.append(data)

        return TYPE5_ROM + self.frame

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


def test_type_five_health_comes_from_the_frame() -> None:
    """Check the rating of a real BL1860B: 4 - (22 + 22 * 6 / 32) / 600."""
    link = TypeFiveLink()
    link.frame = BL1860B
    report = Battery(link=link).report()
    assert report.health_rating == 3.96
    assert report.overdischarge_percent == 0
    assert report.overload_percent == 0


def test_type_five_led_test_is_refused() -> None:
    """Check that the LED test stops before sending anything to a type 5 pack."""
    link = TypeFiveLink()
    with pytest.raises(BatteryError, match='not supported on type 5'):
        Battery(link=link).led_test(seconds=0.0)

    assert link.log == []


class FlakyTypeFiveLink(TypeFiveLink):
    """Gives blank cell replies for the first reads, like some real F0513 variants."""

    def __init__(self, bad_reads: int) -> None:
        """Set the number of blank reads.

        :param bad_reads: Number of blank cell 1 replies.
        """
        super().__init__()
        self.bad_reads = bad_reads

    def skip(self, data: bytes, reply_length: int = 0) -> bytes:
        """Answer a skip ROM transaction.

        :param data: Bytes written.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        if data == b'\x31' and self.bad_reads:
            self.bad_reads -= 1

            return b'\xff\xff'

        return super().skip(data=data, reply_length=reply_length)


def test_type_five_read_retries_until_valid() -> None:
    """Check that a blank read is retried."""
    report = Battery(link=FlakyTypeFiveLink(bad_reads=2)).report()
    assert report.live.cell_voltages == [4.139, 4.137, 4.136, 4.137, 4.137]


def test_type_five_read_gives_up() -> None:
    """Check that a pack that never answers gives no measurements."""
    report = Battery(link=FlakyTypeFiveLink(bad_reads=100)).report()
    assert report.live.cell_voltages == []


def test_type_five_reset_errors_is_refused() -> None:
    """Check that no command is sent to a type 5 pack beyond the frame read."""
    link = TypeFiveLink()
    with pytest.raises(BatteryError, match='not available on type 5'):
        Battery(link=link).reset_errors()

    assert link.log == []


class RecordingLink:
    """Fails the test if a raw command reaches the bus."""

    def __init__(self) -> None:
        """Start with an empty log."""
        self.sent: list[bytes] = []

    def record(self, data: bytes, reply_length: int = 0) -> bytes:
        """Record a command.

        :param data: Bytes written.
        :param reply_length: Number of reply bytes.
        :return: Blank reply.
        """
        self.sent.append(data)

        return b'\xff' * reply_length

    skip = record
    rom = record
    raw = record


@pytest.mark.parametrize(
    ('mode', 'hex_bytes'),
    [
        ('cc', ['99']),
        ('33', ['99']),
        ('none', ['99']),
        ('none', ['cc', '99']),
        ('cc', ['10', '21']),
        ('none', ['cc1021']),
    ],
)
def test_raw_refuses_forbidden_commands_even_with_yes(mode: str, hex_bytes: list[str]) -> None:
    """Check that dangerous commands never reach the bus.

    :param mode: ROM command mode.
    :param hex_bytes: Command bytes.
    """
    link = RecordingLink()
    args = Namespace(bytes=hex_bytes, mode=mode, read=0, yes=True)
    with pytest.raises(BatteryError, match='nothing was sent'):
        cli.run_raw(link=link, args=args)  # type: ignore[arg-type]

    assert link.sent == []


def test_raw_still_allows_reads() -> None:
    """Check that a normal read is sent."""
    link = RecordingLink()
    cli.run_raw(link=link, args=Namespace(bytes=['d7', '0e', '00', '02'], mode='cc', read=3, yes=False))  # type: ignore[arg-type]
    assert link.sent == [bytes.fromhex('d70e0002')]


@pytest.mark.parametrize('command', ['99', '1021'])
def test_raw_refuses_forbidden_commands_after_a_rom_id(command: str) -> None:
    """Check that a hand written ROM id does not hide a dangerous command.

    :param command: Command bytes after the ROM id.
    """
    link = RecordingLink()
    args = Namespace(bytes=['33', '11030102 52ca0737', command], mode='none', read=0, yes=True)
    with pytest.raises(BatteryError, match='nothing was sent'):
        cli.run_raw(link=link, args=args)  # type: ignore[arg-type]

    assert link.sent == []


@pytest.mark.parametrize('command', ['led-test', 'reset-errors'])
def test_type_five_refusal_comes_before_the_confirmation(
    command: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a refused command neither warns nor asks the user.

    :param command: Command name.
    :param capsys: Output capture.
    :param monkeypatch: Patch helper.
    """
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('the user was asked to confirm'))
    link = TypeFiveLink()
    with pytest.raises(BatteryError, match='type 5'):
        cli.dispatch(link=link, args=Namespace(command=command, seconds=0.0, yes=False))  # type: ignore[arg-type]

    assert capsys.readouterr().out == ''
    assert link.log == []


class FixedFrameLink:
    """Answers like the TPCELL pack: a fixed frame to skip ROM, and blank replies to everything else."""

    def __init__(self, frame: bytes = TPCELL) -> None:
        """Set the fixed reply.

        :param frame: Frame returned to skip ROM, FF bytes for no pack at all.
        """
        self.frame = frame
        self.log: list[bytes] = []

    def query(self, command: int, data: bytes = b'', reply_length: int = 0) -> bytes:
        """Answer a firmware frame.

        :param command: Firmware command.
        :param data: Data bytes.
        :param reply_length: Number of reply bytes.
        :return: Blank reply.
        """
        if data != b'\xaa\x00':
            self.log.append(data)

        return b'\xff' * reply_length

    def skip(self, data: bytes, reply_length: int = 0) -> bytes:
        """Answer a skip ROM transaction.

        :param data: Bytes written.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        if data == b'\xaa\x00':
            return self.frame[:reply_length]

        self.log.append(data)

        return b'\xff' * reply_length

    def raw(self, data: bytes, reply_length: int = 0) -> bytes:
        """Answer a raw transaction.

        :param data: Bytes written.
        :param reply_length: Number of reply bytes.
        :return: Blank reply.
        """
        self.log.append(data)

        return b'\xff' * reply_length

    @contextmanager
    def session(self) -> Iterator[None]:
        """Do nothing, the fake pack is always enabled."""
        yield


def test_fixed_frame_pack_is_reported_as_emulated(capsys: pytest.CaptureFixture[str]) -> None:
    """Check that the frame is shown with a warning and no other command is sent.

    :param capsys: Output capture.
    """
    link = FixedFrameLink()
    report = Battery(link=link).report()
    assert report.emulated
    assert report.rom == 'none'
    assert report.cycle_count == 0
    assert report.live == LiveData()
    assert link.log == []
    cli.print_report(report=report)
    assert capsys.readouterr().out.startswith('WARNING: no ROM id')


def test_blank_bus_is_still_a_wiring_error() -> None:
    """Check that no reply at all is not mistaken for a fixed frame pack."""
    with pytest.raises(BatteryError, match='check Enable'):
        Battery(link=FixedFrameLink(frame=b'\xff' * 32)).report()


@pytest.mark.parametrize('command', ['led-test', 'reset-errors', 'restore'])
def test_fixed_frame_pack_refuses_service_commands(
    command: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that service commands stop before the prompt and send nothing.

    :param command: Command name.
    :param capsys: Output capture.
    :param monkeypatch: Patch helper.
    """
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('the user was asked to confirm'))
    link = FixedFrameLink()
    args = Namespace(command=command, seconds=0.0, yes=False, file=Path('backup.json'))
    with pytest.raises(NoRomIdError):
        cli.dispatch(link=link, args=args)  # type: ignore[arg-type]

    assert capsys.readouterr().out == ''
    assert link.log == []


def test_fixed_frame_pack_refuses_unlock() -> None:
    """Check that the unlock plan stops without sending anything."""
    link = FixedFrameLink()
    with pytest.raises(NoRomIdError):
        Battery(link=link).plan_unlock()

    assert link.log == []


def test_fixed_frame_pack_refuses_monitor() -> None:
    """Check that the monitor stops instead of printing empty readings."""
    with pytest.raises(BatteryError, match='monitor needs live data'):
        cli.run_monitor(battery=Battery(link=FixedFrameLink()), args=Namespace(interval=0.0))


class StreamingLink(FixedFrameLink):
    """Answers like the fake BL2110B: one burst after Enable, then blank replies."""

    def __init__(self) -> None:
        """Start with the burst ready."""
        super().__init__(frame=b'\xff' * 32)
        self.burst = bytes.fromhex('FFFFF126BD131458') + BL1860B

    def query(self, command: int, data: bytes = b'', reply_length: int = 0) -> bytes:
        """Answer with the burst once.

        :param command: Firmware command.
        :param data: Data bytes.
        :param reply_length: Number of reply bytes.
        :return: Burst, then blank replies.
        """
        burst, self.burst = self.burst, b''

        return burst or super().query(command=command, data=data, reply_length=reply_length)


def test_streaming_pack_is_not_decoded() -> None:
    """Check that a burst is not reported as a battery."""
    with pytest.raises(StreamingPackError):
        Battery(link=StreamingLink()).report()


@pytest.mark.parametrize('command', ['led-test', 'reset-errors', 'restore'])
def test_streaming_pack_refuses_service_commands(
    command: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that service commands stop before the prompt and send nothing.

    :param command: Command name.
    :param capsys: Output capture.
    :param monkeypatch: Patch helper.
    """
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('the user was asked to confirm'))
    link = StreamingLink()
    args = Namespace(command=command, seconds=0.0, yes=False, file=Path('backup.json'))
    with pytest.raises(StreamingPackError):
        cli.dispatch(link=link, args=args)  # type: ignore[arg-type]

    assert capsys.readouterr().out == ''
    assert link.log == []


def test_streaming_pack_refuses_unlock() -> None:
    """Check that the false lock of a misread burst cannot reach a write."""
    link = StreamingLink()
    with pytest.raises(StreamingPackError):
        Battery(link=link).plan_unlock()

    assert link.log == []


CLEARED = bytes.fromhex('F136B6C318580000949440210180020843D08E1BF064000102020E0000000021')


def test_restore_can_put_back_own_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a cleared pack gets its history back only with the explicit option.

    :param tmp_path: Temporary folder.
    :param monkeypatch: Patch helper.
    """
    monkeypatch.setattr(battery_module, 'BACKUP_DIR', tmp_path / 'backups')
    backup = write_backup(path=tmp_path, frame=GOOD)
    link = FakeLink(frame=CLEARED)
    with pytest.raises(BatteryError, match='beyond the lock fields'):
        Battery(link=link).restore(path=backup)

    assert link.written == []
    assert Battery(link=link).restore(path=backup, allow_history=True).data == GOOD
    saved = json.loads(next((tmp_path / 'backups').glob('*.json')).read_text())
    assert bytes.fromhex(saved['frame']) == CLEARED


def test_restore_history_refuses_a_locked_backup(tmp_path: Path) -> None:
    """Check that an invalid backup is never written.

    :param tmp_path: Temporary folder.
    """
    link = FakeLink(frame=CLEARED)
    with pytest.raises(BatteryError, match='valid backup'):
        Battery(link=link).restore(path=write_backup(path=tmp_path, frame=locked_frame()), allow_history=True)

    assert link.written == []


def test_write_survives_a_missing_exit_acknowledge(tmp_path: Path) -> None:
    """Check that the read back decides, as a real BL1850 gave no acknowledge after the store.

    :param tmp_path: Temporary folder.
    """
    link = FakeLink(frame=CLEARED)
    link.refuse_exit = True
    assert Battery(link=link).restore(path=write_backup(path=tmp_path, frame=GOOD), allow_history=True).data == GOOD


def test_write_reports_a_pack_left_in_test_mode(tmp_path: Path) -> None:
    """Check that a pack still in test mode after a write is reported.

    :param tmp_path: Temporary folder.
    """
    link = FakeLink(frame=CLEARED)
    link.refuse_exit = True
    link.stuck_in_test_mode = True
    with pytest.raises(BatteryError, match='still in test mode'):
        Battery(link=link).restore(path=write_backup(path=tmp_path, frame=GOOD), allow_history=True)
