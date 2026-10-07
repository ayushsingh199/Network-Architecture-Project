"""Header block and REQUEST/RESPONSE/GOAWAY payload codecs."""

from __future__ import annotations

import pytest

from bhp.errors import MalformedPayloadError
from bhp.headers import STATIC_TABLE, decode_headers, encode_headers, get_header
from bhp.messages import (
    GoAway,
    Method,
    Request,
    Response,
    decode_goaway,
    decode_request,
    decode_response,
    encode_goaway,
    encode_request,
    encode_response,
)
from bhp.wire import Cursor


def test_static_table_has_exactly_ten_names() -> None:
    assert len([name for name in STATIC_TABLE if name]) == 10


def test_static_names_cost_one_byte_literals_are_length_prefixed() -> None:
    assert encode_headers([("Host", "a")]) == b"\x01" + b"\x01" + b"\x00\x01a"
    assert encode_headers([("x-y", "b")]) == b"\x01" + b"\x00\x03x-y" + b"\x00\x01b"


def test_header_roundtrip_preserves_order_and_unicode() -> None:
    headers = [("host", "h:1"), ("x-note", "héllo"), ("etag", '"1"'), ("x-note", "two")]
    assert decode_headers(Cursor(encode_headers(headers))) == headers
    assert get_header(headers, "X-Note") == "héllo"
    assert get_header(headers, "missing") is None


@pytest.mark.parametrize(
    "block",
    [
        b"\x01\x0b\x00\x00",  # name index 11 is reserved
        b"\x01\x00\x00\x00\x00",  # empty literal name
        b"\x01\x00\x01A\x00\x00",  # upper-case literal name
        b"\x01\x01\x00\x05ab",  # value shorter than its length
        b"\x02\x01\x00\x00",  # count says two, only one present
        b"\x01\x01\x00\x02a\n",  # LF in value
        b"\x01\x01\x00\x01\xff",  # value not UTF-8
    ],
)
def test_malformed_header_blocks(block: bytes) -> None:
    with pytest.raises(MalformedPayloadError):
        decode_headers(Cursor(block))


@pytest.mark.parametrize("headers", [[("bad name", "v")], [("x", "a\r\nb")], [("", "v")]])
def test_encoder_refuses_what_decoder_would_reject(headers: list[tuple[str, str]]) -> None:
    with pytest.raises(ValueError):
        encode_headers(headers)


def test_request_roundtrip() -> None:
    request = Request(Method.GET, "/a b/ü.html", [("host", "x")])
    assert decode_request(encode_request(request)) == request


@pytest.mark.parametrize(
    "payload",
    [
        b"\x01\x00\x01x\x00",  # path without leading '/'
        b"\x01\x00\x05/ab",  # path truncated
        b"\x01\x00\x01/\x00\xff",  # trailing garbage after header block
        b"\x01\x00\x01/",  # header block missing
        b"",
    ],
)
def test_malformed_requests(payload: bytes) -> None:
    with pytest.raises(MalformedPayloadError):
        decode_request(payload)


def test_unknown_method_decodes_for_a_405() -> None:
    assert decode_request(b"\x09\x00\x01/\x00").method == 9


def test_response_and_goaway_roundtrip() -> None:
    response = Response(404, [("content-length", "0")])
    assert decode_response(encode_response(response)) == response
    goaway = GoAway(7, 400, "bad frame")
    assert decode_goaway(encode_goaway(goaway)) == goaway
    with pytest.raises(MalformedPayloadError):
        decode_response(b"\x00\x63\x00")  # status 99
    with pytest.raises(ValueError):
        encode_response(Response(700))
