"""Serial link to the makita_obi firmware."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

import serial

BAUD_RATE = 9600
BOOT_WAIT_S = 2.5
READ_TIMEOUT_S = 4.0
CMD_VERSION = 0x01
CMD_SESSION = 0x02
CMD_RAW = 0x03
CMD_PRESENCE = 0x04
CMD_ROM = 0x33
CMD_SKIP = 0xCC


class LinkError(Exception):
    """The firmware did not reply as expected."""


class Link:
    """Frame level access to the firmware."""

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

    def query(self, command: int, data: bytes = b'', reply_length: int = 0) -> bytes:
        """Send one frame and return its reply.

        :param command: Firmware command.
        :param data: Data bytes.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        self._device.reset_input_buffer()
        self._device.write(bytes([0x01, len(data), reply_length, command]) + data)
        reply = self._device.read(reply_length + 2)
        if len(reply) != reply_length + 2:
            raise LinkError(f'short reply to command {command:02X}: {reply.hex(" ")}')

        if reply[0] != command or reply[1] != reply_length:
            raise LinkError(f'bad reply header to command {command:02X}: {reply.hex(" ")}')

        return reply[2:]

    def version(self) -> str:
        """Read the firmware version.

        :return: Version text.
        """
        return '.'.join(str(part) for part in self.query(command=CMD_VERSION, reply_length=3))

    def presence(self) -> bool:
        """Check whether a battery answers the bus reset.

        :return: True when a battery answers.
        """
        return bool(self.query(command=CMD_PRESENCE, reply_length=1)[0])

    def skip(self, data: bytes, reply_length: int = 0) -> bytes:
        """Run a skip ROM transaction.

        :param data: Bytes written after the CC byte.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        return self.query(command=CMD_SKIP, data=data, reply_length=reply_length)

    def rom(self, data: bytes, reply_length: int = 0) -> bytes:
        """Run a read ROM transaction.

        :param data: Bytes written after the ROM id.
        :param reply_length: Number of bytes read after the ROM id.
        :return: Bytes read after the ROM id.
        """
        return self.query(command=CMD_ROM, data=data, reply_length=reply_length + 8)[8:]

    def raw(self, data: bytes, reply_length: int = 0) -> bytes:
        """Run a transaction without a ROM command.

        :param data: Bytes written after the bus reset.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        return self.query(command=CMD_RAW, data=data, reply_length=reply_length)

    @contextmanager
    def session(self) -> Iterator[None]:
        """Hold Enable high across several frames."""
        self.query(command=CMD_SESSION, data=b'\x01', reply_length=1)
        try:
            yield
        finally:
            self.query(command=CMD_SESSION, data=b'\x00', reply_length=1)
