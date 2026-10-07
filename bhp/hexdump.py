"""Human-readable rendering of frames: a decoded summary plus a classic hexdump."""

from __future__ import annotations

import logging

from bhp.errors import MalformedPayloadError
from bhp.frame import HEADER_SIZE, Flags, Frame, FrameType
from bhp.messages import decode_goaway, decode_request, decode_response, method_name, reason_phrase

logger = logging.getLogger(__name__)

BYTES_PER_LINE = 16


def hexdump(data: bytes, prefix: str = "", limit: int | None = None) -> str:
    """Render ``data`` as ``offset  hex hex ...  |ascii|`` lines.

    When ``limit`` is set, only the first ``limit`` bytes are shown and a final
    line reports how many were left out.
    """
    shown = data if limit is None else data[:limit]
    lines = []
    for offset in range(0, len(shown), BYTES_PER_LINE):
        chunk = shown[offset : offset + BYTES_PER_LINE]
        left = " ".join(f"{b:02x}" for b in chunk[:8])
        right = " ".join(f"{b:02x}" for b in chunk[8:])
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{prefix}{offset:04x}  {left:<23}  {right:<23}  |{text}|")
    if len(shown) < len(data):
        lines.append(f"{prefix}...   ({len(data) - len(shown)} more bytes not shown)")
    return "\n".join(lines)


def _flag_names(flags: int) -> str:
    names = ["END_STREAM"] if flags & Flags.END_STREAM else []
    unknown = flags & ~int(Flags.END_STREAM) & 0xFF
    if unknown:
        names.append(f"0x{unknown:02x}")
    return "|".join(names) if names else "-"


def describe_frame(frame: Frame) -> list[str]:
    """One summary line plus decoded payload lines for ``frame``."""
    lines = [
        f"{frame.type_name} stream={frame.stream_id} flags={_flag_names(frame.flags)}"
        f" length={len(frame.payload)}"
    ]
    try:
        if frame.type == FrameType.REQUEST:
            request = decode_request(frame.payload)
            lines.append(f"  {method_name(request.method)} {request.path}")
            lines += [f"  {name}: {value}" for name, value in request.headers]
        elif frame.type == FrameType.RESPONSE:
            response = decode_response(frame.payload)
            lines.append(f"  {response.status} {reason_phrase(response.status)}".rstrip())
            lines += [f"  {name}: {value}" for name, value in response.headers]
        elif frame.type == FrameType.GOAWAY:
            goaway = decode_goaway(frame.payload)
            lines.append(
                f"  last_stream={goaway.last_stream_id} code={goaway.code} reason={goaway.reason!r}"
            )
        elif not frame.known:
            lines.append("  (unknown type: skipped by the receiver)")
    except MalformedPayloadError as exc:
        lines.append(f"  (malformed payload: {exc})")
    return lines


def format_frame(direction: str, raw: bytes, frame: Frame, payload_limit: int | None) -> str:
    """Full ``-v`` rendering of one frame travelling in ``direction`` (``>`` or ``<``)."""
    limit = None if payload_limit is None else HEADER_SIZE + payload_limit
    body = [*describe_frame(frame), hexdump(raw, limit=limit)]
    return "\n".join(f"{direction} {line}" for block in body for line in block.split("\n"))
