"""Serial link to the makita_obi firmware."""

import time
from types import TracebackType
from typing import Self

import serial

from makita_tool.transport import FrameLink

BAUD_RATE = 9600
BOOT_WAIT_S = 2.5
READ_TIMEOUT_S = 4.0


class Link(FrameLink):
    """Firmware link over a local serial port."""

    def __init__(self, port: str) -> None:
        """Remember the port name.

        :param port: Serial port path.
        """
        self._device = serial.Serial()
        self._device.port = port
        self._device.baudrate = BAUD_RATE
        self._device.timeout = READ_TIMEOUT_S
        self._device.dtr = False
        self._device.rts = False

    def __enter__(self) -> Self:
        """Open the port and wait for the firmware.

        :return: This link.
        """
        self._device.open()
        time.sleep(BOOT_WAIT_S)
        self._device.reset_input_buffer()

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the port.

        :param exc_type: Exception type.
        :param exc: Exception.
        :param traceback: Traceback.
        """
        self._device.close()

    async def _exchange(self, data: bytes, length: int) -> bytes:
        """Drop pending input, send bytes and read a reply.

        Blocking is fine here: the command line tool does one thing at a time.

        :param data: Bytes to send.
        :param length: Number of bytes to read.
        :return: Bytes read, fewer than requested after a timeout.
        """
        self._device.reset_input_buffer()
        self._device.write(data)

        return self._device.read(length)
