"""Reassemble the engine's raw byte chunks into complete Server-Sent Events.

The judged relay never assumes a raw chunk is one event, or that it ends on a
character boundary: bytes are buffered until a blank line closes an event and
decoded only once that event is complete.
"""

from typing import Final

# exempt: incomplete-event buffer bound, a starting value: no event the engine
# sends approaches it, and an upstream that never closes one is the error path.
SSE_EVENT_BUFFER_LIMIT_BYTES: Final[int] = 1_048_576
DONE_EVENT: Final[str] = "[DONE]"


class SSEEventTooLargeError(ValueError):
    """An incomplete SSE event exceeded the reassembler's byte bound."""


class EventReassembler:
    """Turn raw SSE bytes into complete decoded data payloads."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[str]:
        """Return the payloads completed by one raw byte chunk."""

        self._buffer.extend(chunk)
        events: list[str] = []
        while True:
            boundary = self._boundary()
            if boundary is None:
                self._check_bound()
                break
            event_end, consumed = boundary
            event = bytes(self._buffer[:event_end])
            del self._buffer[:consumed]
            if len(event) > SSE_EVENT_BUFFER_LIMIT_BYTES:
                raise SSEEventTooLargeError(
                    "SSE event exceeded the configured byte bound"
                )
            payload = self._payload(event)
            if payload is not None:
                events.append(payload)
        return events

    def has_pending_bytes(self) -> bool:
        """Return whether a truncated event remains buffered at the stream's end.

        Trailing whitespace closes no event and begins none, so a body that
        ends in a stray newline has ended cleanly, not in part.
        """

        return bool(bytes(self._buffer).strip())

    def _boundary(self) -> tuple[int, int] | None:
        line_feed = self._buffer.find(b"\n\n")
        carriage_return_line_feed = self._buffer.find(b"\r\n\r\n")
        boundaries = [
            (line_feed, 2),
            (carriage_return_line_feed, 4),
        ]
        present = [boundary for boundary in boundaries if boundary[0] >= 0]
        if not present:
            return None
        event_end, separator_length = min(present)
        return event_end, event_end + separator_length

    def _check_bound(self) -> None:
        if len(self._buffer) > SSE_EVENT_BUFFER_LIMIT_BYTES:
            raise SSEEventTooLargeError(
                "SSE event exceeded the configured byte bound"
            )

    @staticmethod
    def _payload(event: bytes) -> str | None:
        decoded = event.decode("utf-8")
        data: list[str] = []
        for line in decoded.split("\n"):
            if line.endswith("\r"):
                line = line[:-1]
            if line.startswith("data:"):
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                data.append(value)
        if not data:
            return None
        return "\n".join(data)
