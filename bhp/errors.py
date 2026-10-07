"""Exception hierarchy that mirrors the two error scopes defined in the spec.

* ``ProtocolError`` — connection scope. Framing can no longer be trusted, so the
  receiver sends GOAWAY (if it can) and closes the connection.
* ``MalformedPayloadError`` — stream scope. The frame boundary is intact, so the
  receiver answers 400 on that stream and keeps the connection open.
"""

from __future__ import annotations


class BHPError(Exception):
    """Base class for every BHP/1 protocol failure."""


class ProtocolError(BHPError):
    """A connection-level error: the byte stream cannot be re-synchronised."""

    def __init__(self, message: str, code: int = 400) -> None:
        """Store the human-readable ``message`` and the GOAWAY ``code`` to send."""
        super().__init__(message)
        self.code = code


class MalformedPayloadError(BHPError):
    """A stream-level error: one frame's payload violates the spec."""
