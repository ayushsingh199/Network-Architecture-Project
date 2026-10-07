"""Bounds-checked big-endian primitives shared by every payload codec."""

from __future__ import annotations

import struct

from bhp.errors import MalformedPayloadError

_U16 = struct.Struct(">H")


class Cursor:
    """Sequential reader over one payload; any overrun raises ``MalformedPayloadError``."""

    def __init__(self, data: bytes) -> None:
        """Start reading ``data`` from offset 0."""
        self._data = data
        self._pos = 0

    @property
    def remaining(self) -> int:
        """Number of unread bytes."""
        return len(self._data) - self._pos

    def take(self, count: int, what: str) -> bytes:
        """Consume exactly ``count`` bytes, naming ``what`` in the error if short."""
        if count > self.remaining:
            raise MalformedPayloadError(
                f"truncated {what}: need {count} bytes, only {self.remaining} left"
            )
        chunk = self._data[self._pos : self._pos + count]
        self._pos += count
        return chunk

    def u8(self, what: str) -> int:
        """Consume one unsigned byte."""
        return self.take(1, what)[0]

    def u16(self, what: str) -> int:
        """Consume one big-endian unsigned 16-bit integer."""
        value: int = _U16.unpack(self.take(2, what))[0]
        return value

    def rest(self) -> bytes:
        """Consume and return every remaining byte."""
        return self.take(self.remaining, "rest")

    def expect_end(self, what: str) -> None:
        """Fail if bytes remain after ``what`` has been fully decoded."""
        if self.remaining:
            raise MalformedPayloadError(f"{self.remaining} trailing bytes after {what}")


def pack_u8(value: int) -> bytes:
    """Encode ``value`` as one unsigned byte, rejecting out-of-range input."""
    if not 0 <= value <= 0xFF:
        raise ValueError(f"u8 out of range: {value}")
    return bytes((value,))


def pack_u16(value: int) -> bytes:
    """Encode ``value`` as a big-endian unsigned 16-bit integer."""
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"u16 out of range: {value}")
    return _U16.pack(value)
