"""The turn harness's raw-stream and probe calls.

The managed-turn recipe belongs to ``gideon.host.owuiturn`` because it is
shared with ``engine verify``; these calls are specific to the tool's probes
and raw-stream replay.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from gideon.host.owui import Client, OwuiError
from gideon.host.owuiturn import COMPLETIONS_PATH, LOGS_FIX
from gideon.host.report import Problem

_NEW_CHAT_PATH: Final = "/api/v1/chats/new"
PROBE_PROMPT: Final[str] = (
    "My client's conviction became final on March 2, 2026 and nothing has been filed since. "
    "On what date is the § 2255 motion due?"
)


@dataclass(frozen=True, slots=True)
class StreamCapture:
    """The released raw-stream deltas, elapsed time, and any stream problem."""

    deltas: tuple[tuple[str, str], ...]
    elapsed: float
    problem: Problem | None = None


@dataclass(frozen=True, slots=True)
class StreamPayload:
    """The delta fields in one engine payload, or its safe parse problem."""

    deltas: tuple[tuple[str, str], ...] = ()
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeResponse:
    """One non-streaming inlet probe response, including only request failures as problems."""

    status: int
    body: object | None
    problem: Problem | None = None


def _probe(client: Client, body: Mapping[str, object]) -> ProbeResponse:
    """Post one non-streaming probe body without converting HTTP responses to errors."""

    try:
        response = client.request("POST", COMPLETIONS_PATH, body)
    except OwuiError as exc:
        return ProbeResponse(0, None, Problem(exc.problem, LOGS_FIX))
    return ProbeResponse(response.status, response.body)


def _bare_body(model: str, prompt: str) -> dict[str, object]:
    """Build the shared non-streaming body without a chat or session id."""

    return {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }


def new_chat(client: Client, model: str) -> str:
    """Create an empty chat the account owns and return its id.

    The pinned frontend answers a completion carrying a chat id it cannot find
    for the caller with a 404 (found on the box, F_0.1.12), so the probe's
    chat id must be a chat the account owns; the frontend's own new-chat route
    makes one without an engine call.
    """

    body = {
        "chat": {
            "title": "New Chat",
            "models": [model],
            "messages": [],
            "history": {"messages": {}, "currentId": None},
        }
    }
    response = client.request("POST", _NEW_CHAT_PATH, body)
    if response.status < 200 or response.status >= 300:
        raise OwuiError(f"Open WebUI {_NEW_CHAT_PATH} returned HTTP {response.status}.")
    if not isinstance(response.body, Mapping):
        raise OwuiError(f"Open WebUI returned an invalid response for {_NEW_CHAT_PATH}.")
    identifier = response.body.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise OwuiError(f"Open WebUI returned an invalid response for {_NEW_CHAT_PATH}.")
    return identifier


def probe_bare(client: Client, model: str, prompt: str) -> ProbeResponse:
    """Probe the inlet without a session or chat id."""

    return _probe(client, _bare_body(model, prompt))


def probe_with_chat_id(
    client: Client, model: str, prompt: str, chat_id: str
) -> ProbeResponse:
    """Probe the inlet with a chat id, the users-seat residual path."""

    return _probe(
        client,
        {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "chat_id": chat_id,
        },
    )


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
                problem = Problem("Open WebUI stream timed out.", LOGS_FIX)
                break
            try:
                payload = next(source)
            except StopIteration:
                break
            except OwuiError as exc:
                problem = Problem(exc.problem, LOGS_FIX)
                break
            parsed = parse_stream_payload(payload)
            if parsed.problem is not None:
                problem = Problem(parsed.problem, LOGS_FIX)
                break
            deltas.extend(parsed.deltas)
    except OwuiError as exc:
        problem = Problem(exc.problem, LOGS_FIX)
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            close()
    elapsed = monotonic() - started
    if problem is None and started + elapsed > deadline:
        problem = Problem("Open WebUI stream timed out.", LOGS_FIX)
    return StreamCapture(tuple(deltas), elapsed, problem)


def parse_stream_payload(payload: object) -> StreamPayload:
    """Extract ordered reasoning and content deltas from one engine payload."""

    if not isinstance(payload, (str, bytes, bytearray)):
        return StreamPayload(problem="the stream carried invalid JSON")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return StreamPayload(problem="the stream carried invalid JSON")
    if not isinstance(decoded, Mapping):
        return StreamPayload(problem="the stream carried a non-object payload")
    if "error" in decoded:
        return StreamPayload(problem="the stream carried an error")
    choices = decoded.get("choices")
    if not isinstance(choices, list) or not choices:
        return StreamPayload(problem="the stream carried no choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return StreamPayload(problem="the stream carried an invalid choice")
    delta = choice.get("delta")
    if not isinstance(delta, Mapping):
        return StreamPayload(problem="the stream carried no delta")
    deltas: list[tuple[str, str]] = []
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
