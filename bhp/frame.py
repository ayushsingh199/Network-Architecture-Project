"""Frame layer: the fixed 8-byte header and whole-frame reading and writing.

Wire layout (network byte order)::

     0                   1                   2                   3
     0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
    +---------------------------------------------------------------+
    |                          Length (32)                          |
    +---------------+---------------+-------------------------------+
    |   Type (8)    |   Flags (8)   |         Stream ID (16)        |
    +---------------+---------------+-------------------------------+
    |                     Payload (Length bytes)                    |
    +---------------------------------------------------------------+
"""

from __future__ import annotations

import logging
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import BinaryIO

from bhp.errors import ProtocolError

logger = logging.getLogger(__name__)

HEADER = struct.Struct(">IBBH")
HEADER_SIZE = HEADER.size
MAX_PAYLOAD = 1 << 24
MAX_STREAM_ID = 0xFFFF
CONNECTION_STREAM = 0
_DISCARD_CHUNK = 64 * 1024


class FrameType(IntEnum):
    """Frame types defined by BHP/1. Every other value MUST be skipped."""

    REQUEST = 0x01
    RESPONSE = 0x02
    DATA = 0x03
    GOAWAY = 0x04


class Flags(IntFlag):
    """Flag bits defined by BHP/1. Undefined bits are ignored on receipt."""

    NONE = 0x00
    END_STREAM = 0x01


@dataclass(frozen=True)
class Frame:
    """One decoded frame. ``type`` stays a raw int so unknown types survive."""

    type: int
    flags: int
    stream_id: int
    payload: bytes

    @property
    def known(self) -> bool:
        """True when ``type`` is one of the BHP/1 frame types."""
        return self.type in FrameType._value2member_map_

    @property
    def end_stream(self) -> bool:
        """True when the END_STREAM flag is set."""
        return bool(self.flags & Flags.END_STREAM)

    @property
    def type_name(self) -> str:
        """Readable name, e.g. ``REQUEST`` or ``UNKNOWN(0x7f)``."""
        return FrameType(self.type).name if self.known else f"UNKNOWN(0x{self.type:02x})"


FrameObserver = Callable[[str, bytes, Frame], None]


def encode_frame(frame_type: int, flags: int, stream_id: int, payload: bytes = b"") -> bytes:
    """Serialise one frame (header + payload), validating every field width."""
    if not 0 <= frame_type <= 0xFF:
        raise ValueError(f"frame type out of range: {frame_type}")
    if not 0 <= flags <= 0xFF:
        raise ValueError(f"flags out of range: {flags}")
    if not 0 <= stream_id <= MAX_STREAM_ID:
        raise ValueError(f"stream id out of range: {stream_id}")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload of {len(payload)} bytes exceeds {MAX_PAYLOAD}")
    return HEADER.pack(len(payload), frame_type, flags, stream_id) + payload


class FrameReader:
    """Reads frames from a blocking binary stream (e.g. ``socket.makefile('rb')``)."""

    def __init__(self, stream: BinaryIO, observer: FrameObserver | None = None) -> None:
        """Wrap ``stream``; ``observer`` (if any) sees every frame received."""
        self._stream = stream
        self._observer = observer

    def _read_exact(self, count: int, what: str) -> bytes:
        data = self._stream.read(count)
        if data is None or len(data) != count:
            got = 0 if data is None else len(data)
            raise ProtocolError(f"connection closed inside {what} ({got}/{count} bytes)")
        return data

    def _discard(self, count: int) -> None:
        while count:
            chunk = self._read_exact(min(count, _DISCARD_CHUNK), "skipped payload")
            count -= len(chunk)

    def read_frame(self, keep_unknown_payload: bool = True) -> Frame | None:
        """Read one frame of any type; return ``None`` on a clean EOF.

        A clean EOF is one that falls exactly on a frame boundary. EOF anywhere
        else, or a Length above ``MAX_PAYLOAD``, raises ``ProtocolError``.
        """
        header = self._stream.read(HEADER_SIZE)
        if not header:
            logger.debug("peer closed the connection on a frame boundary")
            return None
        if len(header) != HEADER_SIZE:
            raise ProtocolError(f"connection closed inside frame header ({len(header)}/8 bytes)")
        length, frame_type, flags, stream_id = HEADER.unpack(header)
        if length > MAX_PAYLOAD:
            raise ProtocolError(f"frame length {length} exceeds limit {MAX_PAYLOAD}")
        is_known = frame_type in FrameType._value2member_map_
        if is_known or keep_unknown_payload:
            payload = self._read_exact(length, "frame payload")
        else:
            self._discard(length)
            payload = b""
        frame = Frame(frame_type, flags, stream_id, payload)
        if self._observer is not None:
            self._observer("<", header + payload, frame)
        return frame

    def read_known_frame(self) -> Frame | None:
        """Read frames until one of a known type arrives, skipping the rest.

        This is the MUST-skip rule: an unknown type is consumed by its Length
        and dropped, leaving the stream aligned on the next frame header.
        """
        while True:
            frame = self.read_frame(keep_unknown_payload=self._observer is not None)
            if frame is None or frame.known:
                return frame
            logger.info(
                "skipping %s on stream %d (%d payload bytes)",
                frame.type_name,
                frame.stream_id,
                len(frame.payload),
            )


class FrameWriter:
    """Writes whole frames to a connected socket."""

    def __init__(self, sock: socket.socket, observer: FrameObserver | None = None) -> None:
        """Wrap ``sock``; ``observer`` (if any) sees every frame before it is sent."""
        self._sock = sock
        self._observer = observer

    def send(self, frame_type: int, flags: int, stream_id: int, payload: bytes = b"") -> None:
        """Encode and send one frame, raising ``OSError`` if the peer is gone."""
        raw = encode_frame(frame_type, flags, stream_id, payload)
        if self._observer is not None:
            self._observer(">", raw, Frame(frame_type, flags, stream_id, payload))
        self._sock.sendall(raw)
