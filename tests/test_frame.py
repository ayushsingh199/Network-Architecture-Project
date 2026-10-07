"""Frame header encoding, size limits and the MUST-skip rule."""

from __future__ import annotations

import io

import pytest

from bhp.errors import ProtocolError
from bhp.frame import HEADER_SIZE, MAX_PAYLOAD, Flags, FrameReader, FrameType, encode_frame


def test_header_is_eight_bytes_big_endian() -> None:
    raw = encode_frame(FrameType.DATA, Flags.END_STREAM, 0x0102, b"abc")
    assert HEADER_SIZE == 8
    assert raw == bytes.fromhex("0000000303010102") + b"abc"


@pytest.mark.parametrize(
    ("ftype", "flags", "stream"), [(256, 0, 1), (1, 256, 1), (1, 0, 65536), (-1, 0, 1)]
)
def test_encode_rejects_out_of_range_fields(ftype: int, flags: int, stream: int) -> None:
    with pytest.raises(ValueError):
        encode_frame(ftype, flags, stream)


def test_roundtrip_and_clean_eof() -> None:
    reader = FrameReader(io.BytesIO(encode_frame(FrameType.REQUEST, 1, 7, b"xy")))
    frame = reader.read_frame()
    assert frame is not None
    assert (frame.type, frame.stream_id, frame.payload, frame.end_stream) == (1, 7, b"xy", True)
    assert reader.read_frame() is None


def test_unknown_types_are_skipped() -> None:
    stream = io.BytesIO(
        encode_frame(0x7F, 0, 1, b"ignore me")
        + encode_frame(0xFF, 0xFF, 0, b"")
        + encode_frame(FrameType.DATA, 0, 3, b"kept")
    )
    frame = FrameReader(stream).read_known_frame()
    assert frame is not None and frame.payload == b"kept" and frame.stream_id == 3


def test_unknown_frame_reported_to_observer() -> None:
    seen: list[str] = []
    stream = io.BytesIO(encode_frame(0x42, 0, 1, b"z") + encode_frame(FrameType.DATA, 0, 1))
    FrameReader(stream, lambda d, raw, f: seen.append(f.type_name)).read_known_frame()
    assert seen == ["UNKNOWN(0x42)", "DATA"]


@pytest.mark.parametrize("data", [b"\x00\x00", encode_frame(FrameType.DATA, 0, 1, b"abcd")[:-1]])
def test_eof_mid_frame_is_a_connection_error(data: bytes) -> None:
    with pytest.raises(ProtocolError):
        FrameReader(io.BytesIO(data)).read_frame()


def test_length_over_limit_is_a_connection_error() -> None:
    header = (MAX_PAYLOAD + 1).to_bytes(4, "big") + b"\x03\x00\x00\x01"
    with pytest.raises(ProtocolError, match="exceeds limit"):
        FrameReader(io.BytesIO(header)).read_frame()
