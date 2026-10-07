"""Path-to-file mapping under the document root."""

from __future__ import annotations

from pathlib import Path

import pytest

from bhp.files import NotFoundError, content_type, resolve


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "index.html"),
        ("/index.html?x=1#frag", "index.html"),
        ("/sub", "sub/index.html"),
        ("/sub/", "sub/index.html"),
        ("/with%20space.txt", "with space.txt"),
        ("//./index.html", "index.html"),
    ],
)
def test_resolves_inside_root(docroot: Path, path: str, expected: str) -> None:
    assert resolve(docroot, path) == (docroot / expected).resolve()


@pytest.mark.parametrize(
    "path", ["/missing", "/../secret.txt", "/sub/../../secret.txt", "/%2e%2e/secret.txt", "/a%00b"]
)
def test_refuses_missing_and_escaping_paths(docroot: Path, path: str) -> None:
    with pytest.raises(NotFoundError):
        resolve(docroot, path)


def test_refuses_symlink_out_of_root(docroot: Path) -> None:
    (docroot / "link.txt").symlink_to(docroot.parent / "secret.txt")
    with pytest.raises(NotFoundError):
        resolve(docroot, "/link.txt")


def test_content_types() -> None:
    assert content_type(Path("a.html")) == "text/html; charset=utf-8"
    assert content_type(Path("a.png")) == "image/png"
    assert content_type(Path("a.unknownext")) == "application/octet-stream"
