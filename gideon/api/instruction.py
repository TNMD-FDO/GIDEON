"""General's instruction placed first in every completion body the service relays.

The rule is the pinned frontend's own for a model preset's system prompt
(``update_message_content``, its non-append branch): a leading system message
with text content keeps its text after General's, joined by a newline, and any
other first message, or none, gets General's text as a new system message
before it. A client can therefore add to the instruction and never remove it.
A body that is not a JSON object with a ``messages`` list is returned as it
stands, since the engine refuses it without serving a completion.
"""

import json

INSTRUCTION_JOINER = "\n"


def instruct_completion_body(body: bytes, instruction: str) -> bytes:
    """Return *body* with General's text first, re-serialized compactly."""

    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return body
    if not isinstance(parsed, dict):
        return body
    messages = parsed.get("messages")
    if not isinstance(messages, list):
        return body

    if (
        messages
        and isinstance(messages[0], dict)
        and messages[0].get("role") == "system"
        and isinstance(messages[0].get("content"), str)
    ):
        messages[0]["content"] = instruction + INSTRUCTION_JOINER + messages[0]["content"]
    else:
        messages.insert(0, {"role": "system", "content": instruction})
    return json.dumps(parsed, separators=(",", ":")).encode("utf-8")
