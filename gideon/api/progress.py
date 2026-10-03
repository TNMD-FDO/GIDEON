"""The progress line's fixed descriptions and the status event that carries them.

The service's stream mechanics send a status event while General's model
reasons, and the turn harness's stream reader and verdict read it back; both
take the forms and the event's shape from here, so the writer and the reader
cannot drift.  It imports the standard library alone: the harness imports it
on the box's system Python, where the service image's dependencies do not
exist.
"""

import re
from typing import Final

# exempt: starting value, a painting cadence: each period of reasoning is one
# more stream message and one more stored status entry, so the entry count and
# the chat row's size a seat's turn shows are what correct it.  A 51-second
# reasoning turn stored 12 entries, 1.4% of its chat row, so the period stands.
PERIOD_SECONDS: Final[int] = 5

# Fixed product text a seat sees and the frontend stores, no site value: the
# word is the frontend's own block's, and nothing but digits the service
# computes from a clock completes the running and closing forms.
OPENING_FORM: Final[str] = "Thinking…"
RUNNING_FORM: Final[str] = "Thinking… {elapsed}"
CLOSING_FORM: Final[str] = "Thought for {elapsed}"

# The names a reasoning delta travels under: the service opens the line on any
# of them and relays none, and the turn harness reads each as a leak.
REASONING_KEYS: Final[tuple[str, ...]] = ("reasoning", "reasoning_content", "thinking")

# The classes of the three forms, so every reader of a painted or stored line
# spells them from one place.
OPENING: Final[str] = "opening"
RUNNING: Final[str] = "running"
CLOSING: Final[str] = "closing"

# The chunk's top-level key the pinned frontend hands to its event emitter.
STATUS_EVENT_KEY: Final[str] = "event"

_ELAPSED_PLACEHOLDER = "{elapsed}"
_SECOND_PATTERN = r"(?:[0-9]|[1-5][0-9])s"
_MINUTE_PATTERN = r"[1-9][0-9]{0,17}m (?:[0-9]|[1-5][0-9])s"
_ELAPSED_PATTERN = "(?:" + _SECOND_PATTERN + "|" + _MINUTE_PATTERN + ")"


def _form_pattern(form: str) -> str:
    return re.escape(form).replace(re.escape(_ELAPSED_PLACEHOLDER), _ELAPSED_PATTERN)


_DESCRIPTION_PATTERNS = (
    (OPENING, re.compile(_form_pattern(OPENING_FORM))),
    (RUNNING, re.compile(_form_pattern(RUNNING_FORM))),
    (CLOSING, re.compile(_form_pattern(CLOSING_FORM))),
)


def elapsed_text(seconds: object) -> str:
    """Render nonnegative whole seconds, clamping an invalid reading to zero."""

    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        seconds = 0
    minutes, remainder = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {remainder}s"
    return f"{remainder}s"


def opening_description() -> str:
    """The line shown at the first reasoning delta."""

    return OPENING_FORM


def running_description(seconds: object) -> str:
    """The line shown at a later reasoning delta."""

    return RUNNING_FORM.format(elapsed=elapsed_text(seconds))


def closing_description(seconds: object) -> str:
    """The line shown when reasoning ends."""

    return CLOSING_FORM.format(elapsed=elapsed_text(seconds))


def is_progress_description(value: object) -> bool:
    """Whether text is exactly one bounded progress form."""

    return classify_description(value) is not None


def classify_description(value: object) -> str | None:
    """Name a complete progress form, or decline text outside the forms."""

    if not isinstance(value, str):
        return None
    for word, pattern in _DESCRIPTION_PATTERNS:
        if pattern.fullmatch(value) is not None:
            return word
    return None


def build_status_event(description: str, done: bool) -> dict[str, object]:
    """Build the frontend status event value for the top-level event key."""

    return {"type": "status", "data": {"description": description, "done": done}}


def read_status_event(value: object) -> str | None:
    """Read a status event's text description, or decline another shape."""

    if not isinstance(value, dict) or value.get("type") != "status":
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        return None
    description = data.get("description")
    return description if isinstance(description, str) else None
