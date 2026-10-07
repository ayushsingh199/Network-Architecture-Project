"""bconform: probe ANY BHP/1 server against the spec's MUST rules.

Point it at your partner's server, not just your own. Every check speaks raw
frames, so it exercises the protocol rather than this repo's client code.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from bhp.client import parse_url
from bhp.errors import BHPError, ProtocolError
from bhp.frame import HEADER, Flags, FrameReader, FrameType, FrameWriter
from bhp.headers import get_header
from bhp.messages import Method, Request, decode_goaway, decode_response, encode_request

logger = logging.getLogger("bconform")


@dataclass
class Reply:
    """Status, headers and body of one complete response."""

    status: int
    headers: list[tuple[str, str]]
    body: bytes


class Probe:
    """A raw connection that can send arbitrary frames and read replies."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        """Connect to ``host:port``."""
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._rfile = self.sock.makefile("rb")
        self.reader = FrameReader(self._rfile)
        self.writer = FrameWriter(self.sock)

    def close(self) -> None:
        """Close the probe's socket."""
        self._rfile.close()
        self.sock.close()

    def request(self, stream_id: int, path: str, method: int = Method.GET, flags: int = 1) -> None:
        """Send a well-formed REQUEST frame."""
        payload = encode_request(Request(method, path, [("host", "conformance")]))
        self.writer.send(FrameType.REQUEST, flags, stream_id, payload)

    def reply(self, stream_id: int) -> Reply:
        """Read one complete response for ``stream_id``; ``ProtocolError`` otherwise."""
        frame = self.reader.read_known_frame()
        if frame is None or frame.type != FrameType.RESPONSE or frame.stream_id != stream_id:
            raise ProtocolError(f"expected RESPONSE on stream {stream_id}, got {frame}")
        response = decode_response(frame.payload)
        body = bytearray()
        while not frame.end_stream:
            frame = self.reader.read_known_frame()
            if frame is None or frame.type != FrameType.DATA or frame.stream_id != stream_id:
                raise ProtocolError(f"expected DATA on stream {stream_id}, got {frame}")
            body += frame.payload
        return Reply(response.status, response.headers, bytes(body))


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_get(probe: Probe, path: str) -> None:
    """GET returns 200 and a body whose size matches content-length."""
    probe.request(1, path)
    reply = probe.reply(1)
    _expect(reply.status == 200, f"status {reply.status}, want 200")
    length = get_header(reply.headers, "content-length")
    _expect(length == str(len(reply.body)), f"content-length {length} != body {len(reply.body)}")


def check_keepalive_404(probe: Probe, _path: str) -> None:
    """A missing file is 404, and the connection survives for the next request."""
    probe.request(3, f"/no-such-file-{uuid.uuid4().hex}")
    _expect(probe.reply(3).status == 404, "missing file did not return 404")


def check_unknown_frame_skipped(probe: Probe, path: str) -> None:
    """An unknown frame type is skipped; the next request is answered normally."""
    probe.writer.send(0xEE, 0, 5, b"future extension payload" * 4)
    probe.request(7, path)
    _expect(probe.reply(7).status == 200, "request after unknown frame failed")


def check_malformed_is_400_and_open(probe: Probe, path: str) -> None:
    """A malformed REQUEST payload earns 400 on that stream, connection stays open."""
    bad = bytes([Method.GET]) + b"\x00\x01/" + b"\x01" + b"\xc8" + b"\x00\x00"
    probe.writer.send(FrameType.REQUEST, Flags.END_STREAM, 9, bad)
    _expect(probe.reply(9).status == 400, "malformed REQUEST did not return 400")
    probe.request(11, path)
    _expect(probe.reply(11).status == 200, "connection unusable after a 400")


def check_head(probe: Probe, path: str) -> None:
    """HEAD returns headers with content-length and no DATA frames."""
    probe.request(13, path, method=Method.HEAD)
    reply = probe.reply(13)
    _expect(reply.status == 200 and not reply.body, "HEAD must be 200 with an empty body")
    _expect(get_header(reply.headers, "content-length") is not None, "HEAD lacks content-length")


def check_unknown_flags_ignored(probe: Probe, path: str) -> None:
    """Undefined flag bits are ignored."""
    probe.request(15, path, flags=Flags.END_STREAM | 0x80)
    _expect(probe.reply(15).status == 200, "undefined flag bit was not ignored")


def check_request_body_drained(probe: Probe, path: str) -> None:
    """A REQUEST followed by DATA frames is answered once the body ends."""
    probe.request(17, path, flags=Flags.NONE)
    probe.writer.send(FrameType.DATA, Flags.NONE, 17, b"part one ")
    probe.writer.send(FrameType.DATA, Flags.END_STREAM, 17, b"part two")
    _expect(probe.reply(17).status == 200, "request with body was not answered")


def check_oversized_frame_goaway(probe: Probe, _path: str) -> None:
    """A Length above the limit is a connection error: GOAWAY then close."""
    probe.sock.sendall(HEADER.pack(0x7FFFFFFF, FrameType.REQUEST, 1, 19))
    frame = probe.reader.read_frame()
    _expect(frame is None or frame.type == FrameType.GOAWAY, f"want GOAWAY or close, got {frame}")
    if frame is not None:
        logger.info("GOAWAY: %s", decode_goaway(frame.payload))
        _expect(probe.reader.read_frame() is None, "connection not closed after GOAWAY")


SHARED_CONNECTION_CHECKS: list[Callable[[Probe, str], None]] = [
    check_get,
    check_keepalive_404,
    check_unknown_frame_skipped,
    check_malformed_is_400_and_open,
    check_head,
    check_unknown_flags_ignored,
    check_request_body_drained,
]


def run(host: str, port: int, path: str, timeout: float = 5.0) -> list[tuple[str, str | None]]:
    """Run every check; return ``(name, failure or None)`` per check."""
    results: list[tuple[str, str | None]] = []
    probe = Probe(host, port, timeout)
    try:
        for check in SHARED_CONNECTION_CHECKS:
            try:
                check(probe, path)
                results.append((check.__name__, None))
            except (AssertionError, BHPError, OSError) as exc:
                results.append((check.__name__, str(exc) or type(exc).__name__))
    finally:
        probe.close()
    fresh = Probe(host, port, timeout)
    try:
        check_oversized_frame_goaway(fresh, path)
        results.append((check_oversized_frame_goaway.__name__, None))
    except (AssertionError, BHPError, OSError) as exc:
        results.append((check_oversized_frame_goaway.__name__, str(exc)))
    finally:
        fresh.close()
    return results


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``bconform host:port/existing-file``; exit 1 if any check fails."""
    parser = argparse.ArgumentParser(prog="bconform", description="BHP/1 server conformance")
    parser.add_argument("url", help="host:port/path-of-a-file-that-exists")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="bconform: %(message)s")
    try:
        target = parse_url(args.url)
        results = run(target.host, target.port, target.path)
    except (ValueError, OSError) as exc:
        print(f"bconform: {exc}", file=sys.stderr)
        return 2
    for name, failure in results:
        print(
            f"{'PASS' if failure is None else 'FAIL'}  {name}"
            + (f"  — {failure}" if failure else "")
        )
    failed = sum(1 for _, failure in results if failure)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0
