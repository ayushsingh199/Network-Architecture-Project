"""Maps request paths to files under a document root, refusing to escape it."""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from urllib.parse import unquote

logger = logging.getLogger(__name__)

INDEX_FILE = "index.html"
DEFAULT_CONTENT_TYPE = "application/octet-stream"


class NotFoundError(Exception):
    """The path does not name a regular file inside the root."""


def resolve(root: Path, request_path: str) -> Path:
    """Map ``request_path`` (e.g. ``/img/logo.png?x=1``) to a file under ``root``.

    Query and fragment are dropped, percent-escapes decoded, and a directory
    maps to its ``index.html``. Any ``..`` segment, NUL byte, or symlink that
    lands outside ``root`` raises ``NotFoundError`` — a 404 reveals nothing about
    what exists outside the root.
    """
    path = request_path.split("?", 1)[0].split("#", 1)[0]
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeDecodeError as exc:
        raise NotFoundError(f"undecodable escape in {request_path!r}") from exc
    if "\x00" in decoded:
        raise NotFoundError("NUL in path")
    parts = [part for part in decoded.split("/") if part not in ("", ".")]
    if ".." in parts:
        logger.warning("refusing traversal attempt %r", request_path)
        raise NotFoundError("'..' segment")
    root_real = root.resolve()
    candidate = root_real.joinpath(*parts).resolve()
    if candidate.is_dir():
        candidate = (candidate / INDEX_FILE).resolve()
    if root_real not in candidate.parents:
        logger.warning("refusing path that escapes the root via symlink: %r", request_path)
        raise NotFoundError("outside root")
    if not candidate.is_file():
        raise NotFoundError(str(candidate))
    return candidate


def content_type(path: Path) -> str:
    """Best-guess MIME type for ``path``; text types get ``charset=utf-8``."""
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed is None:
        return DEFAULT_CONTENT_TYPE
    if guessed.startswith("text/") or guessed in ("application/javascript", "application/json"):
        return f"{guessed}; charset=utf-8"
    return guessed
