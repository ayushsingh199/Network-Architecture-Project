"""Header block codec: a 10-entry static name table plus length-prefixed literals.

Header block layout::

    count (u8)
    count x {
        name_ref (u8)        1..10  -> STATIC_TABLE[name_ref]
                             0      -> literal: name_len (u8) + name bytes
                             11..255 -> reserved, MALFORMED in BHP/1
        value_len (u16) + value bytes (UTF-8)
    }
"""

from __future__ import annotations

import logging

from bhp.errors import MalformedPayloadError
from bhp.wire import Cursor, pack_u8, pack_u16

logger = logging.getLogger(__name__)

Headers = list[tuple[str, str]]

STATIC_TABLE: tuple[str, ...] = (
    "",  # index 0 is the literal marker, never a name
    "host",
    "user-agent",
    "accept",
    "content-type",
    "content-length",
    "server",
    "date",
    "last-modified",
    "allow",
    "etag",
)
LITERAL_NAME = 0
MAX_HEADERS = 0xFF
_STATIC_INDEX = {name: index for index, name in enumerate(STATIC_TABLE) if name}
_NAME_CHARS = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789-_.!#$%&'*+^`|~")
_FORBIDDEN_VALUE_CHARS = ("\x00", "\r", "\n")


def _check_name(name: bytes) -> None:
    if not name:
        raise MalformedPayloadError("empty header name")
    bad = set(name) - _NAME_CHARS
    if bad:
        raise MalformedPayloadError(f"illegal byte(s) in header name {name!r}: {sorted(bad)}")


def _check_value(value: str) -> None:
    if any(ch in value for ch in _FORBIDDEN_VALUE_CHARS):
        raise MalformedPayloadError(f"header value contains NUL/CR/LF: {value!r}")


def encode_headers(headers: Headers) -> bytes:
    """Encode ``headers`` as a header block. Names are lower-cased first.

    Raises ``ValueError`` for anything the decoder would reject, so this side
    can never emit a block its peer must answer with 400.
    """
    if len(headers) > MAX_HEADERS:
        raise ValueError(f"{len(headers)} headers exceeds limit of {MAX_HEADERS}")
    out = bytearray(pack_u8(len(headers)))
    for raw_name, value in headers:
        name = raw_name.strip().lower()
        try:
            _check_name(name.encode("ascii"))
            _check_value(value)
        except (MalformedPayloadError, UnicodeEncodeError) as exc:
            raise ValueError(f"cannot encode header {raw_name!r}: {exc}") from exc
        index = _STATIC_INDEX.get(name)
        if index is not None:
            out += pack_u8(index)
        else:
            encoded_name = name.encode("ascii")
            if len(encoded_name) > 0xFF:
                raise ValueError(f"header name longer than 255 bytes: {name!r}")
            out += pack_u8(LITERAL_NAME) + pack_u8(len(encoded_name)) + encoded_name
        encoded_value = value.encode("utf-8")
        out += pack_u16(len(encoded_value)) + encoded_value
    return bytes(out)


def decode_headers(cursor: Cursor) -> Headers:
    """Decode one header block from ``cursor``; raise ``MalformedPayloadError`` on any violation."""
    count = cursor.u8("header count")
    headers: Headers = []
    for position in range(count):
        ref = cursor.u8(f"name ref of header {position}")
        if ref == LITERAL_NAME:
            raw_name = cursor.take(cursor.u8("literal name length"), "literal name")
            _check_name(raw_name)
            name = raw_name.decode("ascii")
        elif ref < len(STATIC_TABLE):
            name = STATIC_TABLE[ref]
        else:
            raise MalformedPayloadError(f"header name index {ref} is not defined in BHP/1")
        raw_value = cursor.take(cursor.u16("value length"), f"value of {name!r}")
        try:
            value = raw_value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedPayloadError(f"value of {name!r} is not UTF-8") from exc
        _check_value(value)
        headers.append((name, value))
    logger.debug("decoded %d header(s)", len(headers))
    return headers


def get_header(headers: Headers, name: str) -> str | None:
    """Return the first value for ``name`` (case-insensitive), or ``None``."""
    wanted = name.lower()
    return next((value for key, value in headers if key == wanted), None)
