"""Call General through the API service door.

The client source is read once from this package and executed inside
``gideon-api`` with the mounted API key.  The client receives one JSON request
on stdin and writes one JSON object per stdout line.  The envelope's line kinds
are:

* ``head``: the HTTP status and response media type;
* ``body``: the complete non-stream response text;
* ``data``: one event-stream payload and its elapsed offset;
* ``end``: whether the event stream received ``data: [DONE]``;
* ``elapsed``: the complete request duration; and
* ``failure``: the client's exception class, without request or response text.

This module parses both reply forms.  A stream ``data`` line is decoded into
ordered ``(kind, text)`` deltas by :func:`tools.turns.session.parse_stream_payload`;
the enclosing ``DoorEvent`` keeps those deltas with their seconds-from-request
start offset, and the ``end`` line keeps the ``[DONE]`` flag.  A successful
reply is converted to the stored assistant-message shape consumed by
:mod:`tools.turns.classify`.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Final
from urllib.parse import quote

from gideon.host import stack
from gideon.host.engine import RUN_TIMEOUT_MARGIN_SECONDS
from gideon.host.render.api import (
    API_SECRET_NAME,
    API_SERVICE_NAME,
    API_USER_EMAIL_HEADER,
    API_USER_NAME_HEADER,
    API_USER_ROLE_HEADER,
    api_base_url,
)
from gideon.host.render.owui import EVAL_IDENTITY
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike
from tools.turns import session
from tools.turns.doorclient import KEY_FILE_EXIT_CODE

_CLIENT_INTERPRETER: Final[str] = "python3"
_COMPLETIONS_PATH: Final[str] = "/chat/completions"
_MODELS_PATH: Final[str] = "/models"
EVENT_STREAM_MEDIA_TYPE: Final[str] = "text/event-stream"


@dataclass(frozen=True, slots=True)
class DoorEvent:
    """One ordered delta set and its seconds-from-request-start offset."""

    offset: float
    deltas: tuple[tuple[str, str], ...]

    def __repr__(self) -> str:
        """Keep streamed response text out of diagnostic representations."""

        return f"DoorEvent(offset={self.offset!r}, deltas={len(self.deltas)})"


@dataclass(frozen=True, slots=True)
class DoorReply:
    """The parsed API-door envelope, with response text redacted in reprs."""

    status: int | None
    media_type: str
    body_text: str
    events: tuple[DoorEvent, ...]
    done: bool
    elapsed: float | None
    problem: Problem | None

    @property
    def is_stream(self) -> bool:
        """Whether the engine answered as an event stream, whatever was asked."""

        return self.media_type.casefold() == EVENT_STREAM_MEDIA_TYPE

    def __repr__(self) -> str:
        """Keep response and stream text out of diagnostic representations."""

        return (
            "DoorReply("
            f"status={self.status!r}, media_type={self.media_type!r}, "
            "body_text=<redacted>, "
            f"events={len(self.events)}, done={self.done!r}, elapsed={self.elapsed!r}, "
            f"problem={self.problem!r})"
        )


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The models probe's operator-facing outcome."""

    ok: bool
    detail: str
    problem: Problem | None


@cache
def _client_source() -> str:
    """Read the in-container client source once from the turns package."""

    return Path(__file__).with_name("doorclient.py").read_text(encoding="utf-8")


def _api_fix(rendered_dir: PathLike) -> str:
    """Return the fix for an API service or transport failure."""

    return (
        f"Run {stack.logs_fix(rendered_dir, API_SERVICE_NAME)}, then run "
        "sudo python3 -m gideon apply, then retry."
    )


def _key_fix() -> str:
    """Return the fix for a missing, refused, or stale API key."""

    return f"Run sudo python3 -m gideon secrets rotate {API_SECRET_NAME}, then retry."


def _problem(rendered_dir: PathLike, detail: str, *, key: bool = False) -> Problem:
    """Build one content-free door problem with its operator fix."""

    return Problem(detail, _key_fix() if key else _api_fix(rendered_dir))


def identity_headers() -> dict[str, str]:
    """Build the three forwarded eval-identity headers for one request."""

    return {
        API_USER_NAME_HEADER: quote(EVAL_IDENTITY.username, safe=""),
        API_USER_EMAIL_HEADER: EVAL_IDENTITY.email,
        API_USER_ROLE_HEADER: EVAL_IDENTITY.role,
    }


def completion_body(
    *, served_name: str, prompt: str, instruction: str | None, stream: bool
) -> dict[str, object]:
    """Build a direct completion body with General's instruction when present."""

    messages: list[dict[str, str]] = []
    if instruction is not None:
        messages.append({"role": "system", "content": instruction})
    messages.append({"role": "user", "content": prompt})
    return {"model": served_name, "stream": stream, "messages": messages}


def _empty_reply(problem: Problem) -> DoorReply:
    """Return a reply for a request that produced no parseable envelope."""

    return DoorReply(None, "", "", (), False, None, problem)


def _invalid_envelope(rendered_dir: PathLike) -> DoorReply:
    """Return the content-free problem for an invalid envelope."""

    return _empty_reply(_problem(rendered_dir, "door response envelope is invalid"))


def _finite_number(value: object) -> float | None:
    """Return a finite, non-negative JSON number as a float, or ``None``."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _failure_reply(
    rendered_dir: PathLike, record: Mapping[str, object], status: int | None = None
) -> DoorReply:
    """Convert a content-free client failure record into a problem reply."""

    exception = record.get("exception")
    if not isinstance(exception, str) or not exception or not exception.isidentifier():
        return _invalid_envelope(rendered_dir)
    return DoorReply(
        status,
        "",
        "",
        (),
        False,
        None,
        _problem(rendered_dir, f"door client failed: {exception}"),
    )


def _parse_envelope(stdout: str, rendered_dir: PathLike) -> DoorReply:
    """Parse the client's line-delimited envelope without retaining failures' bodies."""

    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return _invalid_envelope(rendered_dir)
    records: list[Mapping[str, object]] = []
    try:
        for line in lines:
            record = json.loads(line)
            if not isinstance(record, Mapping):
                return _invalid_envelope(rendered_dir)
            records.append(record)
    except json.JSONDecodeError:
        return _invalid_envelope(rendered_dir)

    if len(records) == 1 and records[0].get("kind") == "failure":
        return _failure_reply(rendered_dir, records[0])
    head = records[0]
    if head.get("kind") != "head":
        return _invalid_envelope(rendered_dir)
    status = head.get("status")
    media_type = head.get("media_type")
    if type(status) is not int or not isinstance(media_type, str):
        return _invalid_envelope(rendered_dir)

    is_stream = media_type.casefold() == EVENT_STREAM_MEDIA_TYPE
    body_text: str | None = None
    events: list[DoorEvent] = []
    done = False
    ended = False
    stream_problem: Problem | None = None
    for record in records[1:-1]:
        kind = record.get("kind")
        if kind == "body":
            text = record.get("text")
            if is_stream or body_text is not None or not isinstance(text, str):
                return _invalid_envelope(rendered_dir)
            body_text = text
        elif kind == "data":
            offset = _finite_number(record.get("offset"))
            payload = record.get("payload")
            if not is_stream or ended or offset is None or not isinstance(payload, str):
                return _invalid_envelope(rendered_dir)
            parsed = session.parse_stream_payload(payload)
            if parsed.problem is not None:
                stream_problem = _problem(rendered_dir, parsed.problem)
            else:
                events.append(DoorEvent(offset, parsed.deltas))
        elif kind == "end":
            done_value = record.get("done")
            if not is_stream or ended or type(done_value) is not bool:
                return _invalid_envelope(rendered_dir)
            done = done_value
            ended = True
        else:
            return _invalid_envelope(rendered_dir)

    terminal = records[-1]
    terminal_kind = terminal.get("kind")
    if terminal_kind == "failure":
        return _failure_reply(rendered_dir, terminal, status)
    seconds = _finite_number(terminal.get("seconds"))
    if terminal_kind != "elapsed" or seconds is None:
        return _invalid_envelope(rendered_dir)
    if status != 200:
        return DoorReply(
            status,
            media_type,
            "",
            (),
            False,
            seconds,
            _problem(rendered_dir, f"door returned HTTP {status}", key=status == 401),
        )
    if is_stream:
        if not ended or not done:
            return _empty_reply(_problem(rendered_dir, "door stream ended without its end marker"))
        body_text = ""
    elif body_text is None:
        return _invalid_envelope(rendered_dir)

    reply = DoorReply(
        status, media_type, body_text, tuple(events), done, seconds, stream_problem
    )
    return reply


def _run(
    io: Host,
    rendered_dir: PathLike,
    *,
    url: str,
    body: Mapping[str, object] | None,
    max_time: float,
) -> DoorReply:
    """Run the copied client in the API container and parse its envelope."""

    try:
        source = _client_source()
        request = json.dumps({"headers": identity_headers(), "body": body})
    except (OSError, UnicodeError, TypeError, ValueError):
        return _empty_reply(_problem(rendered_dir, "door client source or request is unavailable"))

    # `sh -c script name a b` gives the script $0=name, but `python3 -c src a b`
    # leaves sys.argv[0] as "-c" and makes a name a fourth argument, which the
    # client refuses as usage: the three arguments follow the source directly.
    argv = stack.exec_argv(
        rendered_dir,
        API_SERVICE_NAME,
        _CLIENT_INTERPRETER,
        "-c",
        source,
        f"/run/secrets/{API_SECRET_NAME}",
        url,
        str(max_time),
    )
    try:
        result = io.run(
            argv,
            input=request,
            timeout=max_time + RUN_TIMEOUT_MARGIN_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _empty_reply(_problem(rendered_dir, "door request failed: TimeoutExpired"))
    except OSError:
        return _empty_reply(_problem(rendered_dir, "door request failed: OSError"))
    if result.returncode != 0:
        return _empty_reply(
            _problem(
                rendered_dir,
                f"door request failed: non-zero exit {result.returncode}",
                key=result.returncode == KEY_FILE_EXIT_CODE,
            )
        )
    return _parse_envelope(result.stdout, rendered_dir)


def _completion_message(body_text: str) -> Mapping[str, object] | None:
    """Return the assistant message from a valid whole completion response."""

    try:
        body = json.loads(body_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, Mapping):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return None
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        return None
    if not isinstance(message.get("content"), str):
        return None
    return message


def complete(
    io: Host,
    rendered_dir: PathLike,
    *,
    served_name: str,
    prompt: str,
    instruction: str | None,
    stream: bool,
    max_time: float,
) -> DoorReply:
    """Send one tagged prompt through the door and validate whole replies."""

    reply = _run(
        io,
        rendered_dir,
        url=f"{api_base_url()}{_COMPLETIONS_PATH}",
        body=completion_body(
            served_name=served_name,
            prompt=prompt,
            instruction=instruction,
            stream=stream,
        ),
        max_time=max_time,
    )
    if reply.problem is not None:
        return reply
    if reply.is_stream != stream:
        asked = "streamed" if stream else "whole"
        answered = "streamed" if reply.is_stream else "whole"
        return replace(
            reply,
            body_text="",
            events=(),
            problem=_problem(
                rendered_dir,
                f"door answered a {asked} request with a {answered} response",
            ),
        )
    if stream and stored_message(reply) is None:
        return replace(
            reply,
            body_text="",
            problem=_problem(rendered_dir, "door stream has no assistant message"),
        )
    if stream:
        return reply
    if _completion_message(reply.body_text) is None:
        return replace(
            reply,
            body_text="",
            problem=_problem(rendered_dir, "door completion body has no assistant message"),
        )
    return reply


def stored_message(reply: DoorReply) -> Mapping[str, object] | None:
    """Map a successful whole or streamed completion to classifier input."""

    if reply.problem is not None or reply.status != 200:
        return None
    assistant: dict[str, object]
    if reply.is_stream:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        for event in reply.events:
            for kind, text in event.deltas:
                if kind == "content":
                    content_parts.append(text)
                elif kind == "reasoning":
                    reasoning_parts.append(text)
        assistant = {"content": "".join(content_parts), "output": []}
        stream_reasoning = "".join(reasoning_parts)
        if stream_reasoning:
            assistant["output"] = [
                {
                    "type": "reasoning",
                    "content": [{"type": "output_text", "text": stream_reasoning}],
                }
            ]
        return assistant

    message = _completion_message(reply.body_text)
    if message is None:
        return None
    assistant = {"content": message["content"], "output": []}
    message_reasoning = message.get("reasoning")
    if isinstance(message_reasoning, str) and message_reasoning:
        assistant["output"] = [
            {
                "type": "reasoning",
                "content": [{"type": "output_text", "text": message_reasoning}],
            }
        ]
    return assistant


def probe(
    io: Host,
    rendered_dir: PathLike,
    *,
    served_name: str,
    max_time: float,
) -> ProbeResult:
    """Probe the models route through the door and require the served model."""

    reply = _run(
        io,
        rendered_dir,
        url=f"{api_base_url()}{_MODELS_PATH}",
        body=None,
        max_time=max_time,
    )
    if reply.problem is not None:
        return ProbeResult(False, reply.problem.problem, reply.problem)
    try:
        body = json.loads(reply.body_text)
    except json.JSONDecodeError:
        body = None
    data = body.get("data") if isinstance(body, Mapping) else None
    listed = isinstance(data, list) and any(
        isinstance(model, Mapping) and model.get("id") == served_name for model in data
    )
    if reply.status != 200 or not listed:
        problem = _problem(rendered_dir, "door models response does not list the served model")
        return ProbeResult(False, problem.problem, problem)
    return ProbeResult(True, f"door lists served model {served_name}", None)
