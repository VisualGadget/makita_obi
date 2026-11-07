"""High level battery operations.

Commands come from the public reverse engineering notes in makita-lxt-protocol by rosvall and from
Open Battery Information. Frame write steps were cross-checked against other open projects.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from makita_tool.frame import FRAME_LENGTH, ROM_LENGTH, UNLOCK_NYBBLES, Frame, changed_nybbles, manufacture_date

ACK = 0x06
ACK_REPLY = bytes([ACK])
EXTENDED_READ_LENGTH = 29
MAX_CELL_V = 5.0
MIN_UNLOCK_CELL_V = 3.0
MAX_UNLOCK_IMBALANCE_V = 0.3
MAX_UNLOCK_TEMPERATURE_C = 60.0
COMMIT_WAIT_S = 2.0
STEP_WAIT_S = 0.05
TYPE5_ROM_LIMIT = 100
TYPE5_READ_ATTEMPTS = 5
TYPE5_REFUSALS = {
    'led-test': 'led-test is not supported on type 5: a real BL1860B refused test mode',
    'reset-errors': 'reset-errors is not available on type 5: F0513 packs have no error reset command',
}
BACKUP_DIR = Path.home() / '.local' / 'state' / 'makita_tool' / 'backups'

HEALTH_COMMANDS = {0: bytes.fromhex('d4500102'), 2: bytes.fromhex('d6040502'), 3: bytes.fromhex('d6380202')}
OVERDISCHARGE_COMMANDS = {0: bytes.fromhex('d4ba0001'), 2: bytes.fromhex('d68d0501'), 3: bytes.fromhex('d6090301')}
OVERLOAD_COMMANDS = {
    0: (bytes.fromhex('d48d0007'), 8),
    2: (bytes.fromhex('d65f0507'), 8),
    3: (bytes.fromhex('d65b0304'), 6),
}


class Transport(Protocol):
    """Link operations used by the battery layer."""

    async def skip(self, data: bytes, reply_length: int = 0) -> bytes:
        """Run a skip ROM transaction.

        :param data: Bytes written after the CC byte.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """

    async def raw(self, data: bytes, reply_length: int = 0) -> bytes:
        """Run a transaction without a ROM command.

        :param data: Bytes written after the bus reset.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """

    async def query(self, command: int, data: bytes = b'', reply_length: int = 0) -> bytes:
        """Send one firmware frame.

        :param command: Firmware command.
        :param data: Data bytes.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """

    def session(self) -> AbstractAsyncContextManager[None]:
        """Hold Enable high across several frames."""


class BatteryError(Exception):
    """The battery did not answer, or an operation is not safe."""


class ModeEntryError(BatteryError):
    """The battery did not accept test mode."""


class NoRomIdError(BatteryError):
    """The pack has no ROM id and only answers with a fixed frame."""

    def __init__(self, frame: Frame) -> None:
        """Keep the frame the pack answered with.

        :param frame: Frame read with skip ROM.
        """
        super().__init__(
            'no ROM id: this pack only answers with a fixed frame, so it has no live data or service commands'
        )
        self.frame = frame


class StreamingPackError(BatteryError):
    """The pack sends one unreliable burst after Enable and ignores commands."""

    def __init__(self) -> None:
        """Set the message."""
        super().__init__(
            'not a readable pack: two reads differ, so it has no ROM id and nothing it sends can be decoded. '
            'A third party pack that streams one burst after Enable behaves like this. Check it with a meter'
        )


@dataclass
class LiveData:
    """Measurements read from a running battery."""

    pack_voltage: float | None = None
    cell_voltages: list[float] = field(default_factory=list)
    temperature_c: float | None = None
    temperature2_c: float | None = None

    @property
    def imbalance(self) -> float | None:
        """Return the difference between the highest and lowest cell."""
        return round(max(self.cell_voltages) - min(self.cell_voltages), 3) if self.cell_voltages else None


@dataclass
class Report:
    """Everything known about a battery."""

    rom: str
    frame: str
    manufactured: str
    model: str | None
    model_reported: bool
    controller_type: int | None
    cell_count: int
    capacity_ah: float
    cycle_count: int
    locked: bool
    lock_causes: list[str]
    failure: str
    cell_failure: bool
    damage_rating: int
    live: LiveData
    health_raw: int | None = None
    health_rating: float | None = None
    overdischarge_events: int | None = None
    overload_events: int | None = None
    charge_level_raw: int | None = None
    overdischarge_percent: int | None = None
    overload_percent: int | None = None
    emulated: bool = False


def kelvin_tenths_to_c(raw: int) -> float:
    """Convert a temperature in units of 0.1 K to degrees Celsius.

    :param raw: Raw temperature.
    :return: Temperature in degrees Celsius.
    """
    return round(raw / 10 - 273.15, 2)


def is_blank(reply: bytes) -> bool:
    """Check for the idle bus pattern.

    :param reply: Reply bytes.
    :return: True when every byte is FF or there are no bytes.
    """
    return all(byte == 0xFF for byte in reply)


class Battery:
    """Operations on the connected battery."""

    def __init__(self, link: Transport) -> None:
        """Store the link.

        :param link: Link to the firmware.
        """
        self._link = link

    async def read_frame(self) -> tuple[bytes, Frame]:
        """Read the ROM id and the basic information frame.

        A third party TPCELL pack answers skip ROM with a fixed frame and nothing else, so a blank
        read is only a wiring fault when that frame is missing too. A pack with fake Makita labels sends one burst
        after Enable whatever it is asked, so a second read has to match the first.

        :return: ROM id and frame.
        """
        reply = await self._link.query(command=0x33, data=b'\xaa\x00', reply_length=ROM_LENGTH + FRAME_LENGTH)
        if is_blank(reply):
            fixed = await self._link.skip(data=b'\xaa\x00', reply_length=FRAME_LENGTH)
            if is_blank(fixed):
                raise BatteryError('no data from the battery: check Enable, 1-Wire and ground wiring')

            raise NoRomIdError(frame=Frame(data=fixed))

        if await self._link.query(command=0x33, data=b'\xaa\x00', reply_length=ROM_LENGTH + FRAME_LENGTH) != reply:
            raise StreamingPackError

        return reply[:ROM_LENGTH], Frame(data=reply[ROM_LENGTH:])

    async def _acked(self, command: bytes, reply_length: int) -> bytes | None:
        """Run a command whose reply ends with the acknowledge byte.

        :param command: Bytes written after the CC byte.
        :param reply_length: Reply length, including the acknowledge byte.
        :return: Reply without the acknowledge byte, or None when it is not supported.
        """
        reply = await self._link.skip(data=command, reply_length=reply_length)

        return reply[:-1] if reply[-1] == ACK else None

    async def read_model(self) -> str | None:
        """Read the model string.

        :return: Model, or None when the pack does not report one.
        """
        reply = await self._link.skip(data=b'\xdc\x0c', reply_length=16)
        if is_blank(reply):
            return None

        text = reply.split(b'\x00')[0].decode('ascii', errors='replace').strip()

        return text or None

    async def detect_type(self, rom: bytes, frame: Frame, probe_test_mode: bool = False) -> int | None:
        """Detect the controller type.

        Type 2 can only be confirmed in test mode, so without the probe such packs read as unknown.

        :param rom: ROM id.
        :param frame: Basic information frame.
        :param probe_test_mode: Allow a short visit to test mode.
        :return: Controller type, or None when it is unknown.
        """
        if rom[3] < TYPE5_ROM_LIMIT:
            return 5

        if frame.is_ten_cell:
            return 6

        if await self._acked(command=b'\xdc\x0b', reply_length=17) is not None:
            return 0

        if await self._acked(command=bytes.fromhex('d42c0002'), reply_length=3) is not None:
            return 3

        if probe_test_mode:
            try:
                async with self.test_mode():
                    if await self._acked(command=b'\xdc\x0a', reply_length=17) is not None:
                        return 2

            except ModeEntryError:
                return None

        return None

    async def read_live(self, controller_type: int | None) -> LiveData:
        """Read voltages and temperatures.

        :param controller_type: Controller type.
        :return: Measurements.
        """
        if controller_type == 5:
            return await self._read_live_type5()

        if controller_type == 6:
            return await self._read_live_type6()

        live = LiveData()
        voltages = await self._acked(command=bytes.fromhex('d700000c'), reply_length=13)
        if voltages is not None:
            values = [int.from_bytes(voltages[index : index + 2], 'little') / 1000 for index in range(0, 12, 2)]
            live.pack_voltage = values[0]
            live.cell_voltages = values[1:]

        temperature = await self._acked(command=bytes.fromhex('d70e0002'), reply_length=3)
        if temperature is not None:
            live.temperature_c = kelvin_tenths_to_c(int.from_bytes(temperature, 'little'))

        extended = await self._link.skip(data=bytes.fromhex('d70000ff'), reply_length=EXTENDED_READ_LENGTH)
        if not is_blank(extended[16:18]):
            live.temperature2_c = kelvin_tenths_to_c(int.from_bytes(extended[16:18], 'little'))

        return live

    async def _read_live_type5(self) -> LiveData:
        """Read measurements from an F0513 based pack.

        Sending the F0 00 clear first scrambled these replies on a real BL1860B, and it also changes
        the state of the pack, so it is not used here. Some variants need several reads before all
        cells report valid data.

        :return: Measurements, empty when the replies are not plausible.
        """
        for _ in range(TYPE5_READ_ATTEMPTS):
            cells = []
            for command in (0x31, 0x32, 0x33, 0x34, 0x35):
                cells.append(
                    int.from_bytes(await self._link.skip(data=bytes([command]), reply_length=2), 'little') / 1000
                )

            if all(0 < cell <= MAX_CELL_V for cell in cells):
                break

            await asyncio.sleep(STEP_WAIT_S)

        else:
            return LiveData()

        temperature = int.from_bytes(await self._link.skip(data=b'\x52', reply_length=2), 'little')

        return LiveData(
            pack_voltage=round(sum(cells), 3), cell_voltages=cells, temperature_c=kelvin_tenths_to_c(temperature)
        )

    async def _read_live_type6(self) -> LiveData:
        """Read measurements from a 10-cell pack.

        :return: Measurements.
        """
        await self._link.skip(data=b'\x10\x21')
        reply = await self._link.raw(data=b'\xd4', reply_length=20)
        cells = [(6000 - int.from_bytes(reply[i : i + 2], 'little') / 10) / 1000 for i in range(0, 20, 2)]
        raw = (await self._link.raw(data=b'\xd2', reply_length=1))[0]

        return LiveData(
            pack_voltage=round(sum(cells), 3),
            cell_voltages=[round(cell, 3) for cell in cells],
            temperature_c=round((-40 * raw + 9323) / 100, 2),
        )

    async def report(self, probe_test_mode: bool = False) -> Report:
        """Read everything the pack offers without changing it.

        :param probe_test_mode: Allow a short visit to test mode to identify the controller.
        :return: Battery report.
        """
        async with self._link.session():
            try:
                rom, frame = await self.read_frame()

            except NoRomIdError as error:
                return self._emulated_report(frame=error.frame)

            model = await self.read_model()
            controller_type = await self.detect_type(rom=rom, frame=frame, probe_test_mode=probe_test_mode)
            live = await self.read_live(controller_type=controller_type)
            report = Report(
                rom=rom.hex(' ').upper(),
                frame=frame.data.hex(' ').upper(),
                manufactured=manufacture_date(rom=rom),
                model=model or frame.likely_model,
                model_reported=model is not None,
                controller_type=controller_type,
                cell_count=frame.cell_count,
                capacity_ah=frame.capacity / 10,
                cycle_count=frame.cycle_count,
                locked=bool(frame.lock_causes),
                lock_causes=frame.lock_causes,
                failure=frame.failure_name,
                cell_failure=frame.cell_failure,
                damage_rating=frame.damage_rating,
                live=live,
            )
            if controller_type == 2:
                async with self.test_mode():
                    await self._read_counters(report=report, frame=frame, controller_type=controller_type)
            else:
                await self._read_counters(report=report, frame=frame, controller_type=controller_type)

        return report

    async def refresh(self, report: Report) -> bool:
        """Update the measurements of a report without entering test mode.

        :param report: Report of this pack, updated in place.
        :return: False when the pack or its frame changed, and the report needs a full read.
        """
        async with self._link.session():
            rom, frame = await self.read_frame()
            if rom.hex(' ').upper() != report.rom or frame.data.hex(' ').upper() != report.frame:
                return False

            report.live = await self.read_live(controller_type=report.controller_type)
            if report.controller_type != 2:
                await self._read_counters(report=report, frame=frame, controller_type=report.controller_type)

        return True

    @staticmethod
    def _emulated_report(frame: Frame) -> Report:
        """Describe a pack that only answers with a fixed frame.

        :param frame: Frame read with skip ROM.
        :return: Battery report.
        """
        return Report(
            rom='none',
            frame=frame.data.hex(' ').upper(),
            manufactured='unknown',
            model=frame.likely_model,
            model_reported=False,
            controller_type=None,
            cell_count=frame.cell_count,
            capacity_ah=frame.capacity / 10,
            cycle_count=frame.cycle_count,
            locked=bool(frame.lock_causes),
            lock_causes=frame.lock_causes,
            failure=frame.failure_name,
            cell_failure=frame.cell_failure,
            damage_rating=frame.damage_rating,
            live=LiveData(),
            emulated=True,
        )

    async def _read_counters(self, report: Report, frame: Frame, controller_type: int | None) -> None:
        """Add health and event counters to a report.

        :param report: Report to fill.
        :param frame: Basic information frame.
        :param controller_type: Controller type.
        """
        if controller_type in (5, 6):
            report.health_rating = frame.calculated_health
            report.overdischarge_percent = frame.overdischarge_percent
            report.overload_percent = frame.overload_percent

        if controller_type in (0, None):
            charge = await self._acked(command=bytes.fromhex('d7190004'), reply_length=5)
            report.charge_level_raw = None if charge is None else int.from_bytes(charge, 'little')

        if controller_type not in HEALTH_COMMANDS:
            return

        health = await self._acked(command=HEALTH_COMMANDS[controller_type], reply_length=3)
        if health is not None and frame.capacity:
            report.health_raw = int.from_bytes(health, 'little')
            ratio = report.health_raw / frame.capacity
            report.health_rating = 4.0 if ratio > 80 else round(max(ratio / 10 - 5, 0.0), 1)

        overdischarge = await self._acked(command=OVERDISCHARGE_COMMANDS[controller_type], reply_length=2)
        if overdischarge is not None:
            report.overdischarge_events = overdischarge[0]

        command, length = OVERLOAD_COMMANDS[controller_type]
        overload = await self._acked(command=command, reply_length=length)
        if overload is not None:
            report.overload_events = self._overload_total(reply=overload, controller_type=controller_type)

    @staticmethod
    def _overload_total(reply: bytes, controller_type: int) -> int:
        """Add up the overload counters.

        :param reply: Counter bytes without the acknowledge byte.
        :param controller_type: Controller type.
        :return: Total count.
        """
        if controller_type != 0:
            positions = (0, 2, 3, 5, 6) if controller_type == 2 else (0, 2, 3)

            return sum(reply[position] for position in positions)

        first = (reply[0] >> 6) | (reply[1] << 2)
        second = reply[3] | ((reply[4] & 0b11) << 8)
        third = (reply[5] >> 4) | ((reply[6] & 0b111111) << 4)

        return first + second + third

    @asynccontextmanager
    async def test_mode(self, require_exit_ack: bool = True) -> AsyncIterator[None]:
        """Keep the pack in test mode for a block: it cannot power tools or charge while there.

        :param require_exit_ack: Treat a missing acknowledge on leaving as an error.
        """
        if await self._link.skip(data=bytes.fromhex('d996a5'), reply_length=1) != ACK_REPLY:
            await self._link.skip(data=bytes.fromhex('d9ffff'), reply_length=1)
            raise ModeEntryError('the battery did not accept test mode, nothing was changed')

        await asyncio.sleep(STEP_WAIT_S)
        try:
            yield
        finally:
            await self.exit_test_mode(require_ack=require_exit_ack)

    async def exit_test_mode(self, require_ack: bool = True) -> None:
        """Leave test mode.

        :param require_ack: Treat a missing acknowledge as an error.
        """
        if await self._link.skip(data=bytes.fromhex('d9ffff'), reply_length=1) != ACK_REPLY and require_ack:
            raise BatteryError('no acknowledge when leaving test mode: run exit-test-mode, then check the pack')

        await asyncio.sleep(STEP_WAIT_S)

    async def _rom_step(self, rom: bytes, data: bytes, reply_length: int = 0) -> bytes:
        """Run a read ROM transaction and check that the same battery answered.

        :param rom: Expected ROM id.
        :param data: Bytes written after the ROM id.
        :param reply_length: Number of bytes read after the ROM id.
        :return: Bytes read after the ROM id.
        """
        reply = await self._link.query(command=0x33, data=data, reply_length=ROM_LENGTH + reply_length)
        if reply[:ROM_LENGTH] != rom:
            raise BatteryError(f'ROM id changed during {data[:2].hex(" ")}: {reply[:ROM_LENGTH].hex(" ")}')

        return reply[ROM_LENGTH:]

    @staticmethod
    def _refuse_type5(rom: bytes, command: str) -> None:
        """Stop a command that type 5 packs do not support.

        :param rom: ROM id of the battery.
        :param command: Key in TYPE5_REFUSALS.
        """
        if rom[3] < TYPE5_ROM_LIMIT:
            raise BatteryError(TYPE5_REFUSALS[command])

    async def check_supported(self, command: str) -> None:
        """Read the battery and stop if it does not support a command.

        This only reads, so it can run before the user is asked to confirm.

        :param command: Key in TYPE5_REFUSALS.
        """
        async with self._link.session():
            self._refuse_type5(rom=(await self.read_frame())[0], command=command)

    async def led_test(self, seconds: float) -> bytes:
        """Light all battery LEDs for a while.

        :param seconds: Time the LEDs stay on.
        :return: Reply bytes of the on and off commands.
        """
        async with self._link.session():
            rom = (await self.read_frame())[0]
            self._refuse_type5(rom=rom, command='led-test')
            async with self.test_mode():
                replies = await self._rom_step(rom=rom, data=b'\xda\x31', reply_length=1)
                await asyncio.sleep(seconds)
                replies += await self._rom_step(rom=rom, data=b'\xda\x34', reply_length=1)

        return replies

    async def reset_errors(self) -> None:
        """Clear the error register of the battery, then power cycle it so the change is kept.

        On a real BL1850 this also reset the cycle count and the damage rating to 0.
        The reply byte is not checked: its meaning is not documented, and DA commands answered 07
        on a real BL1850.
        """
        async with self._link.session():
            rom = (await self.read_frame())[0]
            self._refuse_type5(rom=rom, command='reset-errors')
            async with self.test_mode():
                await self._rom_step(rom=rom, data=b'\xda\x04', reply_length=1)

        await asyncio.sleep(COMMIT_WAIT_S)

    async def _write_frame(self, rom: bytes, frame: Frame) -> None:
        """Write a frame to the battery, then power cycle it so the frame is committed.

        The write and store commands have no reply, so the arm reply and the ROM id are the only
        checks available. A failed check stops before the store command. A real BL1850 did not
        acknowledge the exit after a successful store, so a D6 read checks that test mode ended:
        it only answers in test mode on type 2, and other types are untested.

        :param rom: ROM id of the battery.
        :param frame: Frame to write.
        """
        async with self._link.session(), self.test_mode(require_exit_ack=False):
            if is_blank(await self._link.skip(data=b'\xf0\x00', reply_length=FRAME_LENGTH)):
                raise BatteryError('the battery refused to arm: it arms once per insertion, so reinsert it and retry')

            await asyncio.sleep(STEP_WAIT_S)
            await self._rom_step(rom=rom, data=b'\x0f\x00' + frame.data)
            await asyncio.sleep(STEP_WAIT_S)
            await self._rom_step(rom=rom, data=b'\x55\xa5')
            await asyncio.sleep(STEP_WAIT_S)

        await asyncio.sleep(COMMIT_WAIT_S)
        async with self._link.session():
            if not is_blank(await self._link.skip(data=HEALTH_COMMANDS[2], reply_length=2)):
                raise BatteryError('the pack is still in test mode after the write: run exit-test-mode, then check it')

    def unlock_blockers(self, frame: Frame, controller_type: int | None, live: LiveData) -> list[str]:
        """List the reasons an unlock must not run.

        :param frame: Basic information frame.
        :param controller_type: Controller type.
        :param live: Measurements.
        :return: Reasons, empty when the pack may be unlocked.
        """
        blockers = []
        if controller_type is None:
            blockers.append('controller type is unknown: retry with --probe-test-mode')
        elif controller_type not in (0, 2, 3):
            blockers.append(f'unlock is not validated for controller type {controller_type}')

        if frame.failure_code == 0xF:
            blockers.append('the battery reports itself as dead')

        if frame.cell_failure:
            blockers.append('the battery has flagged a cell failure')

        if len(live.cell_voltages) != frame.cell_count:
            blockers.append('cell voltages could not be read')
        else:
            if min(live.cell_voltages) < MIN_UNLOCK_CELL_V:
                blockers.append(f'lowest cell is below {MIN_UNLOCK_CELL_V} V: {min(live.cell_voltages)} V')

            if (live.imbalance or 0) > MAX_UNLOCK_IMBALANCE_V:
                blockers.append(f'cell imbalance is above {MAX_UNLOCK_IMBALANCE_V} V: {live.imbalance} V')

        if live.temperature_c is None or live.temperature_c > MAX_UNLOCK_TEMPERATURE_C:
            blockers.append(f'temperature is unknown or too high: {live.temperature_c}')

        return blockers

    async def plan_unlock(self, probe_test_mode: bool = False) -> tuple[bytes, Frame, Frame | None, bool, list[str]]:
        """Work out what an unlock would do, without writing.

        :param probe_test_mode: Allow a short visit to test mode to identify the controller.
        :return: ROM id, current frame, frame to write or None, whether to clear errors, blockers.
        """
        async with self._link.session():
            rom, frame = await self.read_frame()
            controller_type = await self.detect_type(rom=rom, frame=frame, probe_test_mode=probe_test_mode)
            live = await self.read_live(controller_type=controller_type)

        target = frame.repaired() if frame.lock_causes else None
        clear_errors = frame.failure_code in (0x1, 0x5)
        needs_work = target is not None or clear_errors
        blockers = self.unlock_blockers(frame=frame, controller_type=controller_type, live=live) if needs_work else []

        return rom, frame, target, clear_errors, blockers

    async def _read_back(self) -> Frame:
        """Read the frame in a new session.

        :return: Frame read from the battery.
        """
        async with self._link.session():
            return (await self.read_frame())[1]

    async def unlock(self, probe_test_mode: bool = False) -> Frame:
        """Clear a false lockout, after backing up the frame.

        The error reset runs first, because it needs no frame write. The frame is only written
        when the pack is still locked, and it is built from a fresh read, as the reset can change it.

        :param probe_test_mode: Allow a short visit to test mode to identify the controller.
        :return: Frame read back from the battery.
        """
        rom, frame, target, clear_errors, blockers = await self.plan_unlock(probe_test_mode=probe_test_mode)
        if blockers:
            raise BatteryError('unlock refused: ' + '; '.join(blockers))

        if target is None and not clear_errors:
            return frame

        backup = backup_frame(rom=rom, frame=frame)
        await self.reset_errors()
        result = await self._read_back()
        if result.lock_causes:
            await self._write_frame(rom=rom, frame=result.repaired())
            await self.reset_errors()
            result = await self._read_back()

        if result.lock_causes:
            raise BatteryError(f'still locked: {", ".join(result.lock_causes)}. Backup: {backup}')

        return result

    async def restore(self, path: Path, allow_history: bool = False) -> Frame:
        """Write a backup frame back to the same battery.

        By default only the fields an unlock changes may differ, so a backup can not rewrite the
        cycle count, the failure code or other history of the pack. With allow_history a pack can
        get its own history back, for example after DA 04 cleared it, from a valid backup only.

        :param path: Backup file.
        :param allow_history: Allow the backup to change history fields.
        :return: Frame read back from the battery.
        """
        try:
            saved = json.loads(path.read_text())
            saved_rom = bytes.fromhex(saved['rom'])
            target = Frame(data=bytes.fromhex(saved['frame']))
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise BatteryError(f'cannot use backup {path}: {error}') from error

        async with self._link.session():
            rom, frame = await self.read_frame()
            if rom != saved_rom:
                raise BatteryError(f'backup belongs to another battery: {saved_rom.hex(" ")}')

            changed = set(changed_nybbles(first=frame, second=target)) - set(UNLOCK_NYBBLES)
            if changed and not allow_history:
                raise BatteryError(f'backup differs from the pack beyond the lock fields, nybbles: {sorted(changed)}')

            if allow_history and (target.lock_causes or target.failure_code or frame.failure_code == 0xF):
                raise BatteryError('allow-history needs a valid backup with no failure, on a pack that is not dead')

            if frame == target:
                return frame

            controller_type = await self.detect_type(rom=rom, frame=frame, probe_test_mode=True)

        if controller_type not in (0, 2, 3):
            raise BatteryError(f'restore is not validated for controller type {controller_type}')

        backup = backup_frame(rom=rom, frame=frame)
        await self._write_frame(rom=rom, frame=target)
        result = await self._read_back()
        if result != target:
            raise BatteryError(f'read back differs from the backup. Previous frame: {backup}')

        return result


def backup_frame(rom: bytes, frame: Frame) -> Path:
    """Save a frame before it is changed.

    :param rom: ROM id.
    :param frame: Current frame.
    :return: Path of the backup file.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    path = BACKUP_DIR / f'{rom.hex()}-{stamp}.json'
    path.write_text(json.dumps({'rom': rom.hex(), 'frame': frame.data.hex(), 'saved': stamp}, indent=2) + '\n')

    return path


def report_to_dict(report: Report) -> dict[str, object]:
    """Convert a report to plain data.

    :param report: Battery report.
    :return: Report as a dictionary.
    """
    return asdict(report)
