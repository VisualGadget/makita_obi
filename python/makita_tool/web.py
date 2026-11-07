"""Entry points for the web page, which runs this package in Pyodide.

The page gets reports and the unlock from here so that there is only one implementation. It offers
no raw commands and no restore on purpose.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from makita_tool import __version__
from makita_tool.battery import Battery, BatteryError, NoRomIdError, Report, report_to_dict
from makita_tool.transport import FrameLink, LinkError


class WebLink(FrameLink):
    """Firmware link over a function supplied by the page."""

    def __init__(self, exchange: Callable[[list[int], int], Awaitable[Any]]) -> None:
        """Store the function that talks to the serial port.

        :param exchange: Sends bytes and resolves to the bytes read.
        """
        self._exchange_function = exchange

    async def _exchange(self, data: bytes, length: int) -> bytes:
        """Drop pending input, send bytes and read a reply.

        :param data: Bytes to send.
        :param length: Number of bytes to read.
        :return: Bytes read, fewer than requested after a timeout.
        """
        try:
            reply = await self._exchange_function(list(data), length)
        except Exception as error:
            raise LinkError(f'the serial port failed: {error}') from error

        return bytes(reply.to_py() if hasattr(reply, 'to_py') else reply)


class WebTool:
    """Operations the page offers. Every method returns JSON text, with an error key on failure."""

    def __init__(self, exchange: Callable[[list[int], int], Awaitable[Any]]) -> None:
        """Create the battery layer.

        :param exchange: Sends bytes and resolves to the bytes read.
        """
        self._link = WebLink(exchange=exchange)
        self._battery = Battery(link=self._link)
        self._planned: tuple[bytes, bytes] | None = None
        self._last: Report | None = None

    async def firmware(self) -> str:
        """Read the versions of the firmware and this package.

        :return: JSON text.
        """
        try:
            return json.dumps({'firmware': await self._link.version(), 'tool': __version__})
        except (BatteryError, LinkError) as error:
            return json.dumps({'error': str(error)})

    @staticmethod
    def _report_json(report: Report) -> str:
        """Convert a report for the page.

        :param report: Battery report.
        :return: JSON text.
        """
        return json.dumps({**report_to_dict(report), 'imbalance': report.live.imbalance})

    async def report(self) -> str:
        """Read the battery, with a short visit to test mode when a pack can not be identified without it.

        :return: JSON text.
        """
        self._last = None
        try:
            self._last = await self._battery.report(probe_test_mode=True)
        except (BatteryError, LinkError) as error:
            return json.dumps({'error': str(error)})

        return self._report_json(report=self._last)

    async def monitor(self) -> str:
        """Refresh the measurements, keeping test mode out of the repeated reads.

        :return: JSON text.
        """
        if self._last is None:
            return await self.report()

        try:
            if await self._battery.refresh(report=self._last):
                return self._report_json(report=self._last)

        except NoRomIdError:
            pass

        except (BatteryError, LinkError) as error:
            return json.dumps({'error': str(error)})

        return await self.report()

    async def plan_unlock(self) -> str:
        """Check what an unlock would do, which needs a short visit to test mode on some packs.

        :return: JSON text, including a backup of the current frame.
        """
        try:
            rom, frame, target, clear_errors, blockers = await self._battery.plan_unlock(probe_test_mode=True)
        except (BatteryError, LinkError) as error:
            return json.dumps({'error': str(error)})

        self._planned = (rom, frame.data)

        return json.dumps(
            {
                'frame': frame.data.hex(' ').upper(),
                'target': None if target is None else target.data.hex(' ').upper(),
                'clear_errors': clear_errors,
                'blockers': blockers,
                'failure': frame.failure_name,
                'nothing_to_do': target is None and not clear_errors,
                'backup': {'rom': rom.hex(), 'frame': frame.data.hex()},
            }
        )

    async def unlock(self) -> str:
        """Clear a false lockout, with the same refusals as the command line tool.

        The person agreed to the plan and saved its backup for one pack in one state, so anything
        else is refused. Each plan allows one attempt.

        :return: JSON text with the frame read back.
        """
        planned, self._planned = self._planned, None
        try:
            async with self._link.session():
                rom, current = await self._battery.read_frame()

            if (rom, current.data) != planned:
                raise BatteryError('this pack changed since the check, nothing was written. Check it again')

            frame = await self._battery.unlock(probe_test_mode=True)
        except (BatteryError, LinkError) as error:
            return json.dumps({'error': str(error)})

        return json.dumps(
            {
                'frame': frame.data.hex(' ').upper(),
                'lock_causes': frame.lock_causes,
                'failure': frame.failure_name,
                'cycle_count': frame.cycle_count,
            }
        )
