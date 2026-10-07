"""bcurl: a BHP/1 client. Every URL on the command line shares ONE connection."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from dataclasses import dataclass
from types import TracebackType
from typing import BinaryIO, TextIO

from bhp import PROTOCOL_NAME, __version__
from bhp.errors import MalformedPayloadError, ProtocolError
from bhp.frame import (
    MAX_STREAM_ID,
    Flags,
    Frame,
    FrameObserver,
    FrameReader,
    FrameType,
    FrameWriter,
)
from bhp.headers import Headers
from bhp.hexdump import format_frame
from bhp.messages import (
    Method,
    Request,
    Response,
    decode_goaway,
    decode_response,
    encode_request,
    reason_phrase,
)

logger = logging.getLogger("bcurl")

DEFAULT_PORT = 9000
USER_AGENT = f"bcurl/{__version__}"
VERBOSE_PAYLOAD_LIMIT = 256
EXIT_OK, EXIT_USAGE, EXIT_CONNECTION, EXIT_4XX, EXIT_5XX = 0, 2, 3, 4, 5


@dataclass(frozen=True)
class Target:
    """Where one URL points: ``host``, ``port`` and request ``path``."""

    host: str
    port: int
    path: str

    @property
    def authority(self) -> str:
        """``host:port`` as sent in the ``host`` header."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def parse_url(url: str) -> Target:
    """Parse ``[bhp://]host[:port][/path]``; raise ``ValueError`` if it is unusable."""
    rest = url
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if scheme.lower() != "bhp":
            raise ValueError(f"unsupported scheme {scheme!r} (only bhp:// or none)")
    authority, slash, path = rest.partition("/")
    path = "/" + path if slash else "/"
    if authority.startswith("["):
        host, _, port_text = authority[1:].partition("]")
        port_text = port_text.removeprefix(":")
    else:
        host, _, port_text = authority.partition(":")
    if not host:
        raise ValueError(f"no host in {url!r}")
    if port_text and not port_text.isdigit():
        raise ValueError(f"bad port in {url!r}")
    port = int(port_text) if port_text else DEFAULT_PORT
    if not 0 < port < 65536:
        raise ValueError(f"port out of range in {url!r}")
    return Target(host, port, path)


class ClientConnection:
    """One TCP connection that carries any number of sequential requests."""

    def __init__(
        self, host: str, port: int, timeout: float, observer: FrameObserver | None = None
    ) -> None:
        """Open the connection to ``host:port`` (raises ``OSError`` on failure)."""
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._rfile = self._sock.makefile("rb")
        self._reader = FrameReader(self._rfile, observer)
        self._writer = FrameWriter(self._sock, observer)
        self._stream_id = 0
        logger.debug("connected to %s:%d", host, port)

    def __enter__(self) -> ClientConnection:
        """Context-manager entry."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection on context-manager exit."""
        self.close()

    def close(self) -> None:
        """Close cleanly: a FIN on a frame boundary is a graceful end in BHP/1."""
        self._rfile.close()
        self._sock.close()

    def _next_stream_id(self) -> int:
        self._stream_id = self._stream_id % MAX_STREAM_ID + 1
        return self._stream_id

    def fetch(self, method: int, path: str, headers: Headers, sink: BinaryIO) -> Response:
        """Send one request and copy its body into ``sink``; return the response head.

        Raises ``ProtocolError`` if the server breaks the protocol or goes away.
        """
        stream_id = self._next_stream_id()
        payload = encode_request(Request(method, path, headers))
        self._writer.send(FrameType.REQUEST, Flags.END_STREAM, stream_id, payload)
        response: Response | None = None
        while True:
            frame = self._reader.read_known_frame()
            if frame is None:
                raise ProtocolError("server closed the connection before the response ended")
            response = self._absorb(frame, stream_id, response, sink)
            if frame.end_stream:
                return response

    @staticmethod
    def _absorb(
        frame: Frame, stream_id: int, response: Response | None, sink: BinaryIO
    ) -> Response:
        if frame.type == FrameType.GOAWAY:
            goaway = decode_goaway(frame.payload)
            raise ProtocolError(f"server sent GOAWAY code={goaway.code}: {goaway.reason}")
        if frame.stream_id != stream_id:
            raise ProtocolError(f"frame for stream {frame.stream_id}, expected {stream_id}")
        if frame.type == FrameType.RESPONSE:
            if response is not None:
                raise ProtocolError("second RESPONSE frame on one stream")
            try:
                return decode_response(frame.payload)
            except MalformedPayloadError as exc:
                raise ProtocolError(f"malformed RESPONSE: {exc}") from exc
        if frame.type == FrameType.DATA and response is not None:
            sink.write(frame.payload)
            return response
        raise ProtocolError(f"unexpected {frame.type_name} frame")


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface for ``bcurl``."""
    parser = argparse.ArgumentParser(prog="bcurl", description=f"{PROTOCOL_NAME} client")
    parser.add_argument(
        "urls",
        nargs="+",
        metavar="URL",
        help="[bhp://]host[:port]/path; later URLs may be bare /paths",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="hexdump every frame to stderr (-vv: do not truncate payloads)",
    )
    parser.add_argument("-I", "--head", action="store_true", help="send HEAD, print headers")
    parser.add_argument("-o", "--output", metavar="FILE", help="write body to FILE, not stdout")
    parser.add_argument("-H", "--header", action="append", default=[], metavar="'name: value'")
    parser.add_argument("--timeout", type=float, default=10.0, help="socket timeout in seconds")
    return parser


def _parse_extra_headers(raw: list[str]) -> Headers:
    headers: Headers = []
    for item in raw:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"header must look like 'name: value', got {item!r}")
        headers.append((name.strip().lower(), value.strip()))
    return headers


def _exit_code(worst_status: int) -> int:
    if worst_status >= 500:
        return EXIT_5XX
    return EXIT_4XX if worst_status >= 400 else EXIT_OK


def main(
    argv: list[str] | None = None, stdout: BinaryIO | None = None, stderr: TextIO | None = None
) -> int:
    """Entry point. Exit 0 on success, 4 on any 4xx, 5 on any 5xx, 3 on transport errors."""
    args = build_parser().parse_args(argv)
    out = stdout if stdout is not None else sys.stdout.buffer
    err = stderr if stderr is not None else sys.stderr
    logging.basicConfig(level=logging.WARNING, stream=err, format="bcurl: %(message)s")
    try:
        first = parse_url(args.urls[0])
        targets = [first] + [
            Target(first.host, first.port, url) if url.startswith("/") else parse_url(url)
            for url in args.urls[1:]
        ]
        extra = _parse_extra_headers(args.header)
    except ValueError as exc:
        print(f"bcurl: {exc}", file=err)
        return EXIT_USAGE
    if len({(t.host, t.port) for t in targets}) > 1:
        print(
            "bcurl: all URLs must share one host:port (bcurl never opens a second connection)",
            file=err,
        )
        return EXIT_USAGE

    def observe(direction: str, raw: bytes, frame: Frame) -> None:
        limit = None if args.verbose > 1 else VERBOSE_PAYLOAD_LIMIT
        print(format_frame(direction, raw, frame, limit), file=err, flush=True)

    method = Method.HEAD if args.head else Method.GET
    headers = [("host", first.authority), ("user-agent", USER_AGENT), ("accept", "*/*"), *extra]
    worst = 0
    sink = out
    try:
        if args.output:
            sink = open(args.output, "wb")  # noqa: SIM115 - closed in finally
        observer = observe if args.verbose else None
        with ClientConnection(first.host, first.port, args.timeout, observer) as conn:
            for target in targets:
                response = conn.fetch(method, target.path, headers, sink)
                worst = max(worst, response.status)
                if args.head:
                    lines = [f"{PROTOCOL_NAME} {response.status} {reason_phrase(response.status)}"]
                    lines += [f"{name}: {value}" for name, value in response.headers]
                    out.write(("\n".join(lines) + "\n\n").encode())
                out.flush()
    except (OSError, ProtocolError) as exc:
        print(f"bcurl: {exc}", file=err)
        return EXIT_CONNECTION
    finally:
        if sink is not out:
            sink.close()
    return _exit_code(worst)
