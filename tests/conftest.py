"""Shared fixtures: a live bserve on an ephemeral port and a raw-frame client."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from bhp.conformance import Probe
from bhp.server import BHPServer

INDEX_HTML = b"<!doctype html><title>t</title><p>hello</p>\n"
BIG_SIZE = 200_000


@pytest.fixture()
def docroot(tmp_path: Path) -> Path:
    """A small document root with text, nested, empty and large files."""
    root = tmp_path / "www"
    (root / "sub").mkdir(parents=True)
    (root / "index.html").write_bytes(INDEX_HTML)
    (root / "sub" / "index.html").write_bytes(b"sub index\n")
    (root / "empty.txt").write_bytes(b"")
    (root / "big.bin").write_bytes(bytes(i % 251 for i in range(BIG_SIZE)))
    (root / "with space.txt").write_bytes(b"spaced\n")
    (tmp_path / "secret.txt").write_bytes(b"outside the root\n")
    return root


@pytest.fixture()
def server(docroot: Path) -> Iterator[tuple[str, int]]:
    """Run bserve in a background thread; yield its (host, port)."""
    srv = BHPServer(("127.0.0.1", 0), docroot, idle_timeout=5.0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def probe(server: tuple[str, int]) -> Iterator[Probe]:
    """A raw-frame connection to the running server."""
    connection = Probe(*server, timeout=5.0)
    try:
        yield connection
    finally:
        connection.close()


def is_closed(sock: socket.socket) -> bool:
    """True if the peer has closed ``sock`` (recv returns EOF)."""
    sock.settimeout(2.0)
    return sock.recv(1) == b""
