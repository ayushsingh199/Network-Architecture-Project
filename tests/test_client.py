"""bcurl against bserve and against a scripted fake server."""

from __future__ import annotations

import io
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from bhp.client import EXIT_4XX, EXIT_5XX, EXIT_CONNECTION, EXIT_OK, EXIT_USAGE, main, parse_url
from bhp.frame import Flags, FrameReader, FrameType, FrameWriter
from bhp.messages import Response, encode_response
from tests.conftest import INDEX_HTML


def run(argv: list[str]) -> tuple[int, bytes, str]:
    out, err = io.BytesIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_parse_url() -> None:
    assert parse_url("localhost:9000/index.html").path == "/index.html"
    assert parse_url("bhp://h").port == 9000 and parse_url("bhp://h").path == "/"
    assert parse_url("[::1]:81/x").authority == "[::1]:81"
    for bad in ("http://h/", ":80/", "h:abc/", "h:0/"):
        with pytest.raises(ValueError):
            parse_url(bad)


def test_body_to_stdout(server: tuple[str, int]) -> None:
    code, out, _ = run([f"127.0.0.1:{server[1]}/index.html"])
    assert (code, out) == (EXIT_OK, INDEX_HTML)


def test_verbose_hexdumps_every_frame(server: tuple[str, int]) -> None:
    code, _, err = run(["-v", f"127.0.0.1:{server[1]}/index.html"])
    assert code == EXIT_OK
    assert "> REQUEST stream=1 flags=END_STREAM" in err
    assert "< RESPONSE stream=1" in err and "< DATA stream=1 flags=END_STREAM" in err
    assert "> 0000  00 00 00" in err


def test_404_exits_nonzero(server: tuple[str, int]) -> None:
    code, out, _ = run([f"127.0.0.1:{server[1]}/missing"])
    assert code == EXIT_4XX and out.startswith(b"404 Not Found")


def test_head_prints_headers(server: tuple[str, int]) -> None:
    code, out, _ = run(["-I", f"127.0.0.1:{server[1]}/"])
    assert code == EXIT_OK and out.startswith(b"BHP/1 200 OK\n")
    assert f"content-length: {len(INDEX_HTML)}".encode() in out


def test_output_file(server: tuple[str, int], tmp_path: Path) -> None:
    target = tmp_path / "out.html"
    code, out, _ = run(["-o", str(target), f"127.0.0.1:{server[1]}/"])
    assert code == EXIT_OK and out == b"" and target.read_bytes() == INDEX_HTML


def test_refuses_second_host() -> None:
    code, _, err = run(["a:1/x", "b:1/x"])
    assert code == EXIT_USAGE and "never opens a second connection" in err


def test_connection_refused() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert run([f"127.0.0.1:{port}/"])[0] == EXIT_CONNECTION


@pytest.fixture()
def fake_server() -> Iterator[tuple[int, list[int]]]:
    """Accepts connections, answers each request with an unknown frame, then a 503.

    Yields ``(port, accepted)`` where ``accepted`` counts TCP connections.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    accepted: list[int] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            accepted.append(1)
            with conn, conn.makefile("rb") as rfile:
                reader, writer = FrameReader(rfile), FrameWriter(conn)
                while (frame := reader.read_known_frame()) is not None:
                    writer.send(0x6B, 0, frame.stream_id, b"v2 extension")
                    writer.send(
                        FrameType.RESPONSE,
                        Flags.NONE,
                        frame.stream_id,
                        encode_response(Response(503, [("x-v2", "yes")])),
                    )
                    writer.send(FrameType.DATA, Flags.END_STREAM, frame.stream_id, b"busy")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], accepted
    finally:
        listener.close()


def test_client_skips_unknown_frames_and_uses_one_connection(
    fake_server: tuple[int, list[int]],
) -> None:
    port, accepted = fake_server
    code, out, _ = run([f"127.0.0.1:{port}/a", "/b", f"127.0.0.1:{port}/c"])
    assert code == EXIT_5XX and out == b"busy" * 3
    assert len(accepted) == 1
