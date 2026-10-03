"""The turn harness's raw-stream calls.

The managed-turn recipe belongs to ``gideon.host.owuiturn`` because it is
shared with ``engine verify``; these calls are specific to raw-stream replay.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from gideon.api import progress
from gideon.host.owui import Client, OwuiError, OwuiTimeout
from gideon.host.owuiturn import COMPLETIONS_PATH, LOGS_FIX
from gideon.host.report import Problem, Timeout


@dataclass(frozen=True, slots=True)
class StreamCapture:
    """The released raw-stream deltas, elapsed time, and any stream problem."""

    deltas: tuple[tuple[str, str], ...]
    elapsed: float
    problem: Problem | None = None


@dataclass(frozen=True, slots=True)
class StreamPayload:
    """Ordered status, reasoning, and content deltas, or a safe parse problem."""

    deltas: tuple[tuple[str, str], ...] = ()
    problem: str | None = None


def raw_stream(
    client: Client,
    *,
    model: str,
    prompt: str,
    deadline: float,
    monotonic: Callable[[], float],
) -> StreamCapture:
    """Capture the plain completion stream, retaining partial output on failure.

    The caller supplies the absolute deadline it derived from its turn bound.
    Comparing the final monotonic instant with that deadline also catches a
    stream that reaches ``[DONE]`` only after the bound, without introducing a
    second timeout constant here.
    """

    body = {
        "model": model,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    started = monotonic()
    deltas: list[tuple[str, str]] = []
    problem: Problem | None = None
    source = None
    try:
        source = iter(
            client.stream(
                "POST",
                COMPLETIONS_PATH,
                body,
                deadline=deadline,
                monotonic=monotonic,
            )
        )
        while True:
            if monotonic() >= deadline:
                problem = Timeout("Open WebUI stream timed out.", LOGS_FIX)
                break
            try:
                payload = next(source)
            except StopIteration:
                break
            except OwuiError as exc:
                problem = (
                    Timeout(exc.problem, LOGS_FIX)
                    if isinstance(exc, OwuiTimeout)
                    else Problem(exc.problem, LOGS_FIX)
                )
                break
            parsed = parse_stream_payload(payload)
            if parsed.problem is not None:
                problem = Problem(parsed.problem, LOGS_FIX)
                break
            deltas.extend(parsed.deltas)
    except OwuiError as exc:
        problem = (
            Timeout(exc.problem, LOGS_FIX)
            if isinstance(exc, OwuiTimeout)
            else Problem(exc.problem, LOGS_FIX)
        )
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            close()
    elapsed = monotonic() - started
    if problem is None and started + elapsed > deadline:
        problem = Timeout("Open WebUI stream timed out.", LOGS_FIX)
    return StreamCapture(tuple(deltas), elapsed, problem)


def parse_stream_payload(payload: object) -> StreamPayload:
    """Extract status, reasoning, then content deltas from one stream payload."""

    if not isinstance(payload, (str, bytes, bytearray)):
        return StreamPayload(problem="the stream carried invalid JSON")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return StreamPayload(problem="the stream carried invalid JSON")
    if not isinstance(decoded, Mapping):
        return StreamPayload(problem="the stream carried a non-object payload")
    deltas: list[tuple[str, str]] = []
    if progress.STATUS_EVENT_KEY in decoded:
        description = progress.read_status_event(decoded[progress.STATUS_EVENT_KEY])
        if description is None:
            return StreamPayload(problem="the stream carried an unreadable event")
        deltas.append(("status", description))
    if "error" in decoded:
        return StreamPayload(problem="the stream carried an error")
    choices = decoded.get("choices")
    if not isinstance(choices, list):
        return StreamPayload(problem="the stream carried no choices")
    if not choices:
        if deltas:
            return StreamPayload(tuple(deltas))
        return StreamPayload(problem="the stream carried no choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return StreamPayload(problem="the stream carried an invalid choice")
    delta = choice.get("delta")
    if not isinstance(delta, Mapping):
        return StreamPayload(problem="the stream carried no delta")
    reasoning = delta.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        deltas.append(("reasoning", reasoning))
    content = delta.get("content")
    if isinstance(content, str) and content:
        deltas.append(("content", content))
    return StreamPayload(tuple(deltas))


def sentinel_fragment(sentinel: str) -> str:
    """Return the sentinel-bearing prefix shared by sent prompts and chat reads."""

    return f"[turn harness {sentinel} "


def prompt_text(case_id: str, sentinel: str, prompt: str) -> str:
    """The prompt as sent: the case's text, then a bracketed tag naming the run and the case.

    The tag is what a journal grep finds and what ties a stored chat to its
    case; it trails the prompt in brackets because a leading ``id-sentinel:``
    label read to the generator as a docket or control number (the first seed
    run: "I can’t check that control number…", 39,000 characters of thinking
    about it), which distorts the very turn being measured.
    """

    return f"{prompt}\n\n{sentinel_fragment(sentinel)}{case_id}]"
