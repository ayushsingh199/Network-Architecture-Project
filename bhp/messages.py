"""Payload codecs for the REQUEST, RESPONSE and GOAWAY frame types.

REQUEST  payload: method (u8) | path_len (u16) | path (UTF-8) | header block
RESPONSE payload: status (u16) | header block
GOAWAY   payload: last_stream_id (u16) | code (u16) | reason (UTF-8, rest of payload)
DATA     payload: raw body bytes (no codec needed)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from http import HTTPStatus

from bhp.errors import MalformedPayloadError
from bhp.headers import Headers, decode_headers, encode_headers
from bhp.wire import Cursor, pack_u8, pack_u16

logger = logging.getLogger(__name__)

MAX_PATH_BYTES = 0xFFFF


class Method(IntEnum):
    """Methods defined by BHP/1. Other values decode fine and earn a 405."""

    GET = 0x01
    HEAD = 0x02


def method_name(method: int) -> str:
    """Readable method name, e.g. ``GET`` or ``METHOD(0x09)``."""
    return Method(method).name if method in Method._value2member_map_ else f"METHOD(0x{method:02x})"


def reason_phrase(status: int) -> str:
    """Standard HTTP reason phrase for ``status``, or an empty string."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


@dataclass(frozen=True)
class Request:
    """A decoded REQUEST frame payload."""

    method: int
    path: str
    headers: Headers = field(default_factory=list)


@dataclass(frozen=True)
class Response:
    """A decoded RESPONSE frame payload."""

    status: int
    headers: Headers = field(default_factory=list)


@dataclass(frozen=True)
class GoAway:
    """A decoded GOAWAY frame payload."""

    last_stream_id: int
    code: int
    reason: str = ""


def encode_request(request: Request) -> bytes:
    """Encode a REQUEST payload; ``ValueError`` if it would be malformed."""
    if not request.path.startswith("/") or "\x00" in request.path:
        raise ValueError(f"path must start with '/' and contain no NUL: {request.path!r}")
    path = request.path.encode("utf-8")
    if len(path) > MAX_PATH_BYTES:
        raise ValueError(f"path of {len(path)} bytes exceeds {MAX_PATH_BYTES}")
    return pack_u8(request.method) + pack_u16(len(path)) + path + encode_headers(request.headers)


def decode_request(payload: bytes) -> Request:
    """Decode a REQUEST payload; ``MalformedPayloadError`` on any violation."""
    cursor = Cursor(payload)
    method = cursor.u8("method")
    raw_path = cursor.take(cursor.u16("path length"), "path")
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedPayloadError("path is not valid UTF-8") from exc
    if not path.startswith("/"):
        raise MalformedPayloadError(f"path must start with '/': {path!r}")
    if "\x00" in path:
        raise MalformedPayloadError("path contains NUL")
    headers = decode_headers(cursor)
    cursor.expect_end("REQUEST header block")
    logger.debug("decoded REQUEST %s %s", method_name(method), path)
    return Request(method, path, headers)


def encode_response(response: Response) -> bytes:
    """Encode a RESPONSE payload; ``ValueError`` on an out-of-range status."""
    if not 100 <= response.status <= 599:
        raise ValueError(f"status out of range: {response.status}")
    return pack_u16(response.status) + encode_headers(response.headers)


def decode_response(payload: bytes) -> Response:
    """Decode a RESPONSE payload; ``MalformedPayloadError`` on any violation."""
    cursor = Cursor(payload)
    status = cursor.u16("status")
    if not 100 <= status <= 599:
        raise MalformedPayloadError(f"status out of range: {status}")
    headers = decode_headers(cursor)
    cursor.expect_end("RESPONSE header block")
    logger.debug("decoded RESPONSE %d", status)
    return Response(status, headers)


def encode_goaway(goaway: GoAway) -> bytes:
    """Encode a GOAWAY payload."""
    return pack_u16(goaway.last_stream_id) + pack_u16(goaway.code) + goaway.reason.encode()


def decode_goaway(payload: bytes) -> GoAway:
    """Decode a GOAWAY payload; an undecodable reason is replaced, not rejected."""
    cursor = Cursor(payload)
    last_stream_id = cursor.u16("last stream id")
    code = cursor.u16("code")
    reason = cursor.rest().decode("utf-8", errors="replace")
    return GoAway(last_stream_id, code, reason)
