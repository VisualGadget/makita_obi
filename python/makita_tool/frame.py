"""Decode and repair the 32-byte Makita LXT basic information frame.

The frame is nybble oriented: nybble 0 is the low half of byte 0, nybble 1 is its high half.
"""

from dataclasses import dataclass

FRAME_LENGTH = 32
ROM_LENGTH = 8
FAILURE_NAMES = {0x0: 'OK', 0x1: 'Overloaded', 0x5: 'Warning', 0xF: 'Dead'}
MODEL_BY_CAPACITY = {
    15: 'BL1815N',
    28: 'BL1830 or BL1830B',
    30: 'BL1830 or BL1830B',
    36: 'BL1840 or BL1840B',
    40: 'BL1840 or BL1840B',
    50: 'BL1850 or BL1850B',
    52: 'BL1850 or BL1850B',
    60: 'BL1860B',
}


def checksum(data: bytes | bytearray, first: int, last: int) -> int:
    """Calculate the checksum of a nybble range.

    :param data: Frame bytes.
    :param first: First nybble of the range.
    :param last: Last nybble of the range, inclusive.
    :return: Checksum nybble.
    """
    total = sum(get_nybble(data=data, index=index) for index in range(first, last + 1))

    return min(total, 0xFF) & 0xF


def get_nybble(data: bytes | bytearray, index: int) -> int:
    """Read one nybble.

    :param data: Frame bytes.
    :param index: Nybble index.
    :return: Nybble value.
    """
    value = data[index // 2]

    return value >> 4 if index % 2 else value & 0xF


def set_nybble(data: bytearray, index: int, value: int) -> None:
    """Write one nybble in place.

    :param data: Frame bytes.
    :param index: Nybble index.
    :param value: New nybble value.
    """
    if index % 2:
        data[index // 2] = (data[index // 2] & 0x0F) | ((value & 0xF) << 4)
    else:
        data[index // 2] = (data[index // 2] & 0xF0) | (value & 0xF)


@dataclass(frozen=True)
class Frame:
    """Basic information frame of a battery."""

    data: bytes

    def __post_init__(self) -> None:
        """Validate the frame length."""
        if len(self.data) != FRAME_LENGTH:
            raise ValueError(f'frame must have {FRAME_LENGTH} bytes, got {len(self.data)}')

    def _pair(self, index: int) -> int:
        """Read a value stored as a high nybble followed by a low nybble.

        :param index: Index of the high nybble.
        :return: 8-bit value.
        """
        return (get_nybble(data=self.data, index=index) << 4) | get_nybble(data=self.data, index=index + 1)

    @property
    def battery_type(self) -> int:
        """Return the raw type value: 13 to 29 means a 5-cell pack."""
        return self._pair(22)

    @property
    def capacity(self) -> int:
        """Return the capacity in units of 0.1 Ah."""
        return self._pair(32)

    @property
    def flags(self) -> int:
        """Return the flags value."""
        return self._pair(34)

    @property
    def charger_lock(self) -> int:
        """Return the charger lock nybble: anything but 0 stops charging."""
        return get_nybble(data=self.data, index=34)

    @property
    def failure_code(self) -> int:
        """Return the failure code."""
        return get_nybble(data=self.data, index=40)

    @property
    def failure_name(self) -> str:
        """Return the failure code as text."""
        return FAILURE_NAMES.get(self.failure_code, f'Unknown ({self.failure_code:X})')

    @property
    def cell_failure(self) -> bool:
        """Return True when the battery has flagged a cell failure."""
        return bool(get_nybble(data=self.data, index=44) & 0b0100)

    @property
    def damage_rating(self) -> int:
        """Return the damage rating: 0 is best, 7 is worst."""
        return get_nybble(data=self.data, index=46) >> 1

    @property
    def overdischarge(self) -> int:
        """Return the raw overdischarge value."""
        return self._pair(48)

    @property
    def overload(self) -> int:
        """Return the raw overload value."""
        return self._pair(50)

    @property
    def cycle_count(self) -> int:
        """Return the number of charge cycles."""
        nybbles = [get_nybble(data=self.data, index=index) for index in range(52, 56)]

        return ((nybbles[0] & 1) << 12) | (nybbles[1] << 8) | (nybbles[2] << 4) | nybbles[3]

    @property
    def cell_count(self) -> int:
        """Return the number of cells implied by the type value."""
        if self.is_ten_cell:
            return 10

        return 4 if self.battery_type < 13 else 5

    @property
    def is_ten_cell(self) -> bool:
        """Return True for a 10-cell pack."""
        return self.battery_type == 30 or self.flags == 0x1E or self.data[17] == 30

    @property
    def lock_causes(self) -> list[str]:
        """Return every reason the frame reads as locked, empty when it does not."""
        causes = []
        if self.charger_lock:
            causes.append(f'charger lock nybble is {self.charger_lock:X}')

        for name, stored, first, last in (('CS0', 41, 0, 15), ('CS1', 42, 16, 31), ('CS2', 43, 32, 40)):
            if get_nybble(data=self.data, index=stored) != checksum(data=self.data, first=first, last=last):
                causes.append(f'{name} checksum mismatch')

        return causes

    @property
    def likely_model(self) -> str | None:
        """Return the model suggested by the capacity, for packs that do not report one."""
        return MODEL_BY_CAPACITY.get(self.capacity) if self.cell_count == 5 else None

    def repaired(self) -> 'Frame':
        """Return a copy with the charger lock cleared and the lock checksums recalculated.

        The failure code and all history fields stay as they are, so a pack the battery itself
        has marked as faulty is not disguised as healthy.

        :return: Repaired frame.
        """
        data = bytearray(self.data)
        set_nybble(data=data, index=34, value=0)
        set_nybble(data=data, index=41, value=checksum(data=data, first=0, last=15))
        set_nybble(data=data, index=42, value=checksum(data=data, first=16, last=31))
        set_nybble(data=data, index=43, value=checksum(data=data, first=32, last=40))

        return Frame(data=bytes(data))


def manufacture_date(rom: bytes) -> str:
    """Decode the manufacturing date stored in the ROM id.

    :param rom: ROM id bytes.
    :return: Date in ISO format.
    """
    return f'20{rom[0]:02d}-{rom[1]:02d}-{rom[2]:02d}'
