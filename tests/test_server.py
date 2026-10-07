"""bserve end-to-end over real sockets, speaking raw frames."""

from __future__ import annotations

import socket

from bhp.conformance import Probe
from bhp.frame import HEADER, Flags, FrameType
from bhp.headers import get_header
from bhp.messages import Method, decode_goaway
from tests.conftest import BIG_SIZE, INDEX_HTML, is_closed


def test_get_index(probe: Probe) -> None:
    probe.request(1, "/")
    reply = probe.reply(1)
    assert reply.status == 200 and reply.body == INDEX_HTML
    assert get_header(reply.headers, "content-type") == "text/html; charset=utf-8"
    assert get_header(reply.headers, "content-length") == str(len(INDEX_HTML))
    for name in ("server", "date", "last-modified", "etag"):
        assert get_header(reply.headers, name)


def test_many_requests_share_one_connection(probe: Probe) -> None:
    for stream_id, path, status in [(1, "/", 200), (2, "/nope", 404), (3, "/sub/", 200)]:
        probe.request(stream_id, path)
        assert probe.reply(stream_id).status == status


def test_large_file_is_split_into_data_frames(probe: Probe) -> None:
    probe.request(1, "/big.bin")
    frame = probe.reader.read_known_frame()
    assert frame is not None and frame.type == FrameType.RESPONSE and not frame.end_stream
    sizes, body = [], bytearray()
    while not frame.end_stream:
        frame = probe.reader.read_known_frame()
        assert frame is not None and frame.type == FrameType.DATA
        sizes.append(len(frame.payload))
        body += frame.payload
    assert len(sizes) == 4 and max(sizes) == 64 * 1024
    assert bytes(body) == bytes(i % 251 for i in range(BIG_SIZE))


def test_empty_file_ends_on_the_response_frame(probe: Probe) -> None:
    probe.request(1, "/empty.txt")
    frame = probe.reader.read_known_frame()
    assert frame is not None and frame.type == FrameType.RESPONSE and frame.end_stream


def test_head_has_no_body(probe: Probe) -> None:
    probe.request(1, "/big.bin", method=Method.HEAD)
    reply = probe.reply(1)
    assert reply.status == 200 and reply.body == b""
    assert get_header(reply.headers, "content-length") == str(BIG_SIZE)


def test_traversal_is_404(probe: Probe) -> None:
    probe.request(1, "/../secret.txt")
    assert probe.reply(1).status == 404


def test_unknown_method_is_405_with_allow(probe: Probe) -> None:
    probe.request(1, "/", method=0x09)
    reply = probe.reply(1)
    assert reply.status == 405 and get_header(reply.headers, "allow") == "GET, HEAD"


def test_malformed_payload_is_400_and_connection_stays_open(probe: Probe) -> None:
    probe.writer.send(FrameType.REQUEST, Flags.END_STREAM, 1, b"\x01\x00\x01x\x00")
    reply = probe.reply(1)
    assert reply.status == 400 and b"must start with '/'" in reply.body
    probe.request(2, "/")
    assert probe.reply(2).status == 200


def test_stray_response_frame_is_400(probe: Probe) -> None:
    probe.writer.send(FrameType.RESPONSE, Flags.END_STREAM, 4, b"\x00\xc8\x00")
    assert probe.reply(4).status == 400


def test_unknown_frames_are_skipped(probe: Probe) -> None:
    probe.writer.send(0x99, 0xFF, 1, b"\x00" * 1000)
    probe.writer.send(0x05, 0, 0, b"")
    probe.request(2, "/")
    assert probe.reply(2).status == 200


def test_request_on_stream_zero_is_goaway(probe: Probe) -> None:
    probe.request(0, "/")
    frame = probe.reader.read_frame()
    assert frame is not None and frame.type == FrameType.GOAWAY and frame.stream_id == 0
    assert decode_goaway(frame.payload).code == 400
    assert is_closed(probe.sock)


def test_text_http_is_rejected_with_goaway(server: tuple[str, int]) -> None:
    with socket.create_connection(server, timeout=5) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        data = sock.recv(4096)
        length, ftype, _flags, stream = HEADER.unpack(data[:8])
        assert ftype == FrameType.GOAWAY and stream == 0
        assert b"exceeds limit" in data[8 : 8 + length]


def test_client_goaway_closes_connection(probe: Probe) -> None:
    probe.writer.send(FrameType.GOAWAY, 0, 0, b"\x00\x00\x00\x00")
    assert is_closed(probe.sock)
