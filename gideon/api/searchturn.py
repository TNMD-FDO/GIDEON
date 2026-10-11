"""Recognize search-query task calls and keep their content-free event rows."""

import json
from dataclasses import dataclass
from typing import Final

from gideon import guardrail

# The pinned frontend's v0.11.3 default in backend/open_webui/config.py:2322-2323.
# GIDEON renders no query template, so this opening identifies its task call.
SEARCH_QUERY_TELL: Final[str] = (
    "### Task:\n"
    "Analyze the chat history to determine the necessity of generating search queries"
)
SEARCH_TURNS_TABLE: Final[str] = "search_turns"
SEARCH_TURNS_PARTITION_FUNCTION: Final[str] = "search_turns_ensure_partition"
SEARCH_TURNS_COLUMNS: Final[tuple[str, ...]] = ("user_id", "chat_id", "source")


@dataclass(frozen=True, slots=True)
class SearchTurnRow:
    """The content-free row persisted for one searched turn."""

    user_id: str | None
    chat_id: str | None
    source: str


def is_search_query_call(body: bytes) -> bool:
    """Match the first user message's text against the frontend's task opening."""

    try:
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            return False
        messages = parsed.get("messages")
        if not isinstance(messages, list):
            return False
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.startswith(SEARCH_QUERY_TELL)
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text")
                        return isinstance(text, str) and text.startswith(SEARCH_QUERY_TELL)
            return False
        return False
    except Exception:  # noqa: BLE001 - unreadable bodies never identify a search call.
        return False


def record_search_turn(user_id: str | None, chat_id: str | None, source: str) -> None:
    """Dispatch bounded ids and a source word without affecting the response."""

    try:
        row = SearchTurnRow(
            guardrail.bounded_id(user_id),
            guardrail.bounded_id(chat_id),
            source if source in guardrail.SOURCE_VOCABULARY else guardrail.USER_SOURCE,
        )
        guardrail.dispatch_event_row(
            SEARCH_TURNS_TABLE,
            SEARCH_TURNS_PARTITION_FUNCTION,
            SEARCH_TURNS_COLUMNS,
            (row.user_id, row.chat_id, row.source),
        )
    except Exception:  # noqa: BLE001 - recording cannot affect the response.
        return
