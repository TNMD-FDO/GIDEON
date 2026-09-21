"""Unit contracts for Server-Sent Events byte reassembly."""

import unittest

from gideon.api.sse import (
    DONE_EVENT,
    SSE_EVENT_BUFFER_LIMIT_BYTES,
    EventReassembler,
    SSEEventTooLargeError,
)


class ApiSSE(unittest.TestCase):
    def test_event_split_at_every_byte_offset(self) -> None:
        event = b'data: {"fixture":"answer"}\n\n'

        for offset in range(len(event) + 1):
            with self.subTest(offset=offset):
                reassembler = EventReassembler()
                payloads = reassembler.feed(event[:offset])
                self.assertEqual(
                    reassembler.has_pending_bytes(), 0 < offset < len(event)
                )
                payloads.extend(reassembler.feed(event[offset:]))

                self.assertEqual(payloads, ['{"fixture":"answer"}'])
                self.assertFalse(reassembler.has_pending_bytes())

    def test_multi_byte_character_split_across_raw_chunks(self) -> None:
        event = "data: fixture café\n\n".encode()
        split = event.index("é".encode()) + 1
        reassembler = EventReassembler()

        self.assertEqual(reassembler.feed(event[:split]), [])
        self.assertEqual(reassembler.feed(event[split:]), ["fixture café"])
        self.assertFalse(reassembler.has_pending_bytes())

    def test_several_events_in_one_chunk(self) -> None:
        chunk = b"data: first\n\ndata: second\n\ndata: [DONE]\n\n"

        self.assertEqual(
            EventReassembler().feed(chunk), ["first", "second", DONE_EVENT]
        )

    def test_crlf_framing(self) -> None:
        event = b"data: fixture\r\n\r\n"
        reassembler = EventReassembler()

        self.assertEqual(reassembler.feed(event), ["fixture"])
        self.assertFalse(reassembler.has_pending_bytes())

    def test_comments_and_unused_fields_are_dropped(self) -> None:
        event = b": keep-alive\ndata: first\nevent: ignored\ndata: second\nid: ignored\n\n"

        self.assertEqual(EventReassembler().feed(event), ["first\nsecond"])

    def test_an_event_with_no_data_line_yields_nothing(self) -> None:
        reassembler = EventReassembler()

        self.assertEqual(reassembler.feed(b": keep-alive\n\n"), [])
        self.assertEqual(reassembler.feed(b'data: {"fixture":1}\n\n'), ['{"fixture":1}'])

    def test_trailing_whitespace_is_not_a_truncated_event(self) -> None:
        reassembler = EventReassembler()

        self.assertEqual(reassembler.feed(b"data: fixture\n\n\n"), ["fixture"])
        self.assertFalse(reassembler.has_pending_bytes())

    def test_a_truncated_last_event_is_still_pending(self) -> None:
        reassembler = EventReassembler()

        self.assertEqual(reassembler.feed(b"data: fixture\n\ndata: cut"), ["fixture"])
        self.assertTrue(reassembler.has_pending_bytes())

    def test_mixed_framing_across_one_chunk(self) -> None:
        chunk = b"data: first\r\n\r\ndata: second\n\n"

        self.assertEqual(EventReassembler().feed(chunk), ["first", "second"])

    def test_event_buffer_bound_raises_distinct_exception(self) -> None:
        reassembler = EventReassembler()
        oversized = b"data: " + b"x" * SSE_EVENT_BUFFER_LIMIT_BYTES

        with self.assertRaises(SSEEventTooLargeError):
            reassembler.feed(oversized)


if __name__ == "__main__":
    unittest.main()
