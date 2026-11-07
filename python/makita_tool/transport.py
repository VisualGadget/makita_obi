"""Frame protocol of the makita_obi firmware, independent of how the bytes travel."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

CMD_VERSION = 0x01
CMD_SESSION = 0x02
CMD_RAW = 0x03
CMD_PRESENCE = 0x04
CMD_ROM = 0x33
CMD_SKIP = 0xCC


class LinkError(Exception):
    """The firmware did not reply as expected."""


class FrameLink(ABC):
    """Frame level access to the firmware."""

    @abstractmethod
    async def _exchange(self, data: bytes, length: int) -> bytes:
        """Drop pending input, send bytes and read a reply.

        :param data: Bytes to send.
        :param length: Number of bytes to read.
        :return: Bytes read, fewer than requested after a timeout.
        """

    async def query(self, command: int, data: bytes = b'', reply_length: int = 0) -> bytes:
        """Send one frame and return its reply.

        :param command: Firmware command.
        :param data: Data bytes.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        reply = await self._exchange(
            data=bytes([0x01, len(data), reply_length, command]) + data,
            length=reply_length + 2,
        )
        if len(reply) != reply_length + 2:
            raise LinkError(f'short reply to command {command:02X}: {reply.hex(" ")}')

        if reply[0] != command or reply[1] != reply_length:
            raise LinkError(f'bad reply header to command {command:02X}: {reply.hex(" ")}')

        return reply[2:]

    async def version(self) -> str:
        """Read the firmware version.

        :return: Version text.
        """
        return '.'.join(str(part) for part in await self.query(command=CMD_VERSION, reply_length=3))

    async def presence(self) -> bool:
        """Check whether a battery answers the bus reset.

        :return: True when a battery answers.
        """
        return bool((await self.query(command=CMD_PRESENCE, reply_length=1))[0])

    async def skip(self, data: bytes, reply_length: int = 0) -> bytes:
        """Run a skip ROM transaction.

        :param data: Bytes written after the CC byte.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        return await self.query(command=CMD_SKIP, data=data, reply_length=reply_length)

    async def rom(self, data: bytes, reply_length: int = 0) -> bytes:
        """Run a read ROM transaction.

        :param data: Bytes written after the ROM id.
        :param reply_length: Number of bytes read after the ROM id.
        :return: Bytes read after the ROM id.
        """
        return (await self.query(command=CMD_ROM, data=data, reply_length=reply_length + 8))[8:]

    async def raw(self, data: bytes, reply_length: int = 0) -> bytes:
        """Run a transaction without a ROM command.

        :param data: Bytes written after the bus reset.
        :param reply_length: Number of reply bytes.
        :return: Reply bytes.
        """
        return await self.query(command=CMD_RAW, data=data, reply_length=reply_length)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        """Hold Enable high across several frames."""
        await self.query(command=CMD_SESSION, data=b'\x01', reply_length=1)
        try:
            yield
        finally:
            await self.query(command=CMD_SESSION, data=b'\x00', reply_length=1)
