"""bserve: a BHP/1 static file server. One thread per TCP connection, kept open."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import socketserver
import sys
from email.utils import formatdate
from pathlib import Path
from typing import BinaryIO

from bhp import PROTOCOL_NAME, __version__
from bhp.errors import MalformedPayloadError, ProtocolError
from bhp.files import NotFoundError, content_type, resolve
from bhp.frame import CONNECTION_STREAM, Flags, Frame, FrameReader, FrameType, FrameWriter
from bhp.headers import Headers
from bhp.messages import (
    GoAway,
    Method,
    Request,
    Response,
    decode_request,
    encode_goaway,
    encode_response,
    method_name,
    reason_phrase,
)

logger = logging.getLogger("bserve")

SERVER_NAME = f"bserve/{__version__}"
CHUNK_SIZE = 64 * 1024
DEFAULT_IDLE_TIMEOUT = 30.0
ALLOWED_METHODS = "GET, HEAD"


class ConnectionHandler:
    """Serves every request that arrives on one TCP connection, in order."""

    def __init__(self, sock: socket.socket, peer: str, root: Path, idle_timeout: float) -> None:
        """Bind the handler to ``sock``; ``idle_timeout`` seconds of silence closes it."""
        sock.settimeout(idle_timeout)
        self._sock = sock
        self._peer = peer
        self._root = root
        self._rfile = sock.makefile("rb")
        self._reader = FrameReader(self._rfile)
        self._writer = FrameWriter(sock)
        self._last_stream = CONNECTION_STREAM

    def run(self) -> None:
        """Read and answer frames until EOF, GOAWAY, idle timeout or a connection error."""
        logger.info("%s connected", self._peer)
        try:
            while True:
                frame = self._reader.read_known_frame()
                if frame is None or not self._dispatch(frame):
                    break
        except TimeoutError:
            self._goaway(0, "idle timeout")
        except ProtocolError as exc:
            logger.warning("%s connection error: %s", self._peer, exc)
            self._goaway(exc.code, str(exc))
        except OSError as exc:
            logger.info("%s socket error: %s", self._peer, exc)
        finally:
            self._rfile.close()
            logger.info("%s disconnected", self._peer)

    def _dispatch(self, frame: Frame) -> bool:
        """Handle one known frame; return False when the connection should end."""
        if frame.type == FrameType.REQUEST:
            self._on_request(frame)
            return True
        if frame.type == FrameType.GOAWAY:
            logger.info("%s sent GOAWAY; closing", self._peer)
            return False
        if frame.stream_id == CONNECTION_STREAM:
            raise ProtocolError(f"{frame.type_name} frame on stream 0")
        self._send_error(frame.stream_id, 400, f"unexpected {frame.type_name} frame", head=False)
        return True

    def _drain_request_body(self, stream_id: int) -> None:
        """Consume (and ignore) DATA frames that follow a REQUEST without END_STREAM."""
        while True:
            frame = self._reader.read_known_frame()
            if frame is None:
                raise ProtocolError("connection closed inside a request body")
            if frame.type != FrameType.DATA or frame.stream_id != stream_id:
                raise ProtocolError(f"expected DATA on stream {stream_id}, got {frame.type_name}")
            if frame.end_stream:
                return

    def _on_request(self, frame: Frame) -> None:
        if frame.stream_id == CONNECTION_STREAM:
            raise ProtocolError("REQUEST frame on stream 0")
        self._last_stream = frame.stream_id
        if not frame.end_stream:
            self._drain_request_body(frame.stream_id)
        try:
            request = decode_request(frame.payload)
        except MalformedPayloadError as exc:
            logger.info("%s stream=%d malformed REQUEST: %s", self._peer, frame.stream_id, exc)
            self._send_error(frame.stream_id, 400, str(exc), head=False)
            return
        self._serve(frame.stream_id, request)

    def _serve(self, stream_id: int, request: Request) -> None:
        head = request.method == Method.HEAD
        if request.method not in (Method.GET, Method.HEAD):
            self._send_error(
                stream_id,
                405,
                f"{method_name(request.method)} not supported",
                head=False,
                extra=[("allow", ALLOWED_METHODS)],
            )
            return
        try:
            path = resolve(self._root, request.path)
            handle = path.open("rb")
        except NotFoundError:
            self._send_error(stream_id, 404, request.path, head=head)
            return
        except PermissionError:
            self._send_error(stream_id, 403, request.path, head=head)
            return
        except OSError as exc:
            logger.error("%s cannot open %s: %s", self._peer, request.path, exc)
            self._send_error(stream_id, 500, "cannot read file", head=head)
            return
        with handle:
            stat = os.fstat(handle.fileno())
            headers: Headers = [
                ("content-type", content_type(path)),
                ("content-length", str(stat.st_size)),
                ("last-modified", formatdate(stat.st_mtime, usegmt=True)),
                ("etag", f'"{stat.st_size:x}-{int(stat.st_mtime):x}"'),
            ]
            self._send_response(stream_id, 200, headers, end=head or stat.st_size == 0)
            sent = 0 if head else self._stream_file(stream_id, handle, stat.st_size)
        self._log(stream_id, request, 200, sent)

    def _stream_file(self, stream_id: int, handle: BinaryIO, size: int) -> int:
        """Send ``size`` bytes of ``handle`` as DATA frames; the last carries END_STREAM."""
        remaining = size
        while remaining:
            chunk = handle.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise ProtocolError("file shrank while it was being sent", code=500)
            remaining -= len(chunk)
            flags = Flags.END_STREAM if remaining == 0 else Flags.NONE
            self._writer.send(FrameType.DATA, flags, stream_id, chunk)
        return size

    def _send_response(self, stream_id: int, status: int, headers: Headers, end: bool) -> None:
        common: Headers = [("server", SERVER_NAME), ("date", formatdate(usegmt=True))]
        payload = encode_response(Response(status, common + headers))
        flags = Flags.END_STREAM if end else Flags.NONE
        self._writer.send(FrameType.RESPONSE, flags, stream_id, payload)

    def _send_error(
        self, stream_id: int, status: int, detail: str, head: bool, extra: Headers | None = None
    ) -> None:
        body = f"{status} {reason_phrase(status)}: {detail}\n".encode()
        headers: Headers = [
            ("content-type", "text/plain; charset=utf-8"),
            ("content-length", str(len(body))),
            *(extra or []),
        ]
        self._send_response(stream_id, status, headers, end=head)
        if not head:
            self._writer.send(FrameType.DATA, Flags.END_STREAM, stream_id, body)
        logger.info("%s stream=%d -> %d (%s)", self._peer, stream_id, status, detail)

    def _goaway(self, code: int, reason: str) -> None:
        try:
            payload = encode_goaway(GoAway(self._last_stream, code, reason[:200]))
            self._writer.send(FrameType.GOAWAY, Flags.NONE, CONNECTION_STREAM, payload)
        except OSError as exc:
            logger.debug("%s could not send GOAWAY: %s", self._peer, exc)

    def _log(self, stream_id: int, request: Request, status: int, sent: int) -> None:
        logger.info(
            "%s stream=%d %s %s -> %d (%d body bytes)",
            self._peer,
            stream_id,
            method_name(request.method),
            request.path,
            status,
            sent,
        )


class BHPServer(socketserver.ThreadingTCPServer):
    """Threaded TCP server that hands each connection to a ``ConnectionHandler``."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: Path, idle_timeout: float) -> None:
        """Bind to ``address`` and serve files from ``root``."""
        if not root.is_dir():
            raise NotADirectoryError(f"document root is not a directory: {root}")
        self.root = root
        self.idle_timeout = idle_timeout
        super().__init__(address, _RequestHandler)


class _RequestHandler(socketserver.BaseRequestHandler):
    server: BHPServer

    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        ConnectionHandler(self.request, peer, self.server.root, self.server.idle_timeout).run()


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface for ``bserve``."""
    parser = argparse.ArgumentParser(prog="bserve", description=f"{PROTOCOL_NAME} file server")
    parser.add_argument("root", type=Path, help="directory to serve")
    parser.add_argument("port", type=int, help="TCP port to listen on")
    parser.add_argument("--host", default="0.0.0.0", help="address to bind (default 0.0.0.0)")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT)
    parser.add_argument("-q", "--quiet", action="store_true", help="log warnings only")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments and serve until interrupted."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        server = BHPServer((args.host, args.port), args.root, args.idle_timeout)
    except (OSError, NotADirectoryError) as exc:
        logger.error("cannot start: %s", exc)
        return 1
    with server:
        logger.info("serving %s on %s:%d", args.root.resolve(), args.host, args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("shutting down")
    return 0
