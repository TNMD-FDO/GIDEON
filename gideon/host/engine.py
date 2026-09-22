"""The direct serving-engine verification command (§6.7)."""

import argparse
import json
import math
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]

from gideon import guardrail
from gideon.host import (
    apply,
    enginesample,
    models,
    nogpu,
    owui,
    owuiturn,
    secrets,
    site,
    stack,
    tls,
)
from gideon.host import audit as audit_module
from gideon.host.render.engine import (
    ENGINE_PORT,
    ENGINE_SECRET_NAME,
    ENGINE_SERVICE_NAME,
)
from gideon.host.render.owui import (
    EVAL_IDENTITY,
    EVAL_PASSWORD_SECRET,
    GENERAL_PRESET_ID,
)
from gideon.host.report import Problem, StageResult, print_stage
from gideon.host.sysio import Host, PathLike, RealHost

_SITE_PATH: Final[str] = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final[str] = "/etc/gideon/rendered"
_ROOT_FIX: Final[str] = "Run sudo python3 -m gideon engine verify."
_APPLY_FIX: Final[str] = "Run sudo python3 -m gideon apply, then retry."
_SAMPLE_FIX: Final[str] = (
    "Edit eval/engine-verify/sample.yaml, then re-run sudo python3 -m gideon engine verify."
)
_FRONTEND_DELETE_FIX: Final[str] = (
    "Check the eval account's chat list in the frontend, then retry."
)
_MODELS_FIX: Final[str] = "Edit models.lock, then re-run sudo python3 -m gideon engine verify."
_FILLER_FIX: Final[str] = (
    "Adjust needle.filler in eval/engine-verify/sample.yaml to a paragraph the engine "
    "tokenizes to a steady count, then re-run sudo python3 -m gideon engine verify."
)
# curl's own diagnostic is safe to show (never a response body); one line, bounded.
_STDERR_LIMIT: Final[int] = 200

# exempt: acceptance bounds (ADR-0017).  The smoke is a bounded operator check;
# the timing is a starting value, not a pass threshold.
SMOKE_TIMEOUT_SECONDS: Final[int] = 120
# exempt: acceptance bounds (ADR-0017).  Host.run must outlive curl's bound.
RUN_TIMEOUT_MARGIN_SECONDS: Final[int] = 15
# §6.7's nominal needle lengths (32k, 128k, 256k): a spec decision, not a starting value.
NEEDLE_LENGTHS: Final[tuple[int, ...]] = (32_768, 131_072, 262_144)
# exempt: acceptance bounds (ADR-0017).  The bounded recall answer reserve.
NEEDLE_MAX_TOKENS: Final[int] = 64
# exempt: acceptance bounds (ADR-0017).  A deterministic needle request seed.
NEEDLE_SEED: Final[int] = 1
# exempt: acceptance bounds (ADR-0017).  Reserve beyond the answer bound.
NEEDLE_SIZING_MARGIN_TOKENS: Final[int] = 256
# exempt: acceptance bounds (ADR-0017).  Minimum exact-fill fraction.
NEEDLE_MIN_FILL: Final[float] = 0.97
# exempt: acceptance bounds (ADR-0017).  Maximum correction rounds.
NEEDLE_SIZING_ROUNDS: Final[int] = 6
# exempt: acceptance bounds (ADR-0017).  Tokenization request bound.
TOKENIZE_TIMEOUT_SECONDS: Final[int] = 60
# exempt: acceptance bounds (ADR-0017).  Per-needle request bound.
NEEDLE_TIMEOUT_SECONDS: Final[int] = 600
# exempt: acceptance bounds (ADR-0017).  The structured-output request bound.
STRUCTURED_TIMEOUT_SECONDS: Final[int] = 180
# exempt: acceptance bounds (ADR-0017).  The reasoning and JSON answer reserve.
STRUCTURED_MAX_TOKENS: Final[int] = 2048
# exempt: acceptance bounds (ADR-0017). The starting value is
# gideon.evaluation.turns.run's TURN_TIMEOUT_SECONDS; ticket 52 measured 245 s of
# thinking on one positive.
FRONTEND_TURN_TIMEOUT_SECONDS: Final[int] = 600
FRONTEND_ROW_PREFIX: Final[str] = "frontend-"

# The empty Expect header stops curl's 100-continue handshake on a body over a
# kilobyte (a needle prompt is a megabyte), so no request waits on the server's
# interim reply. The image's curl (7.81) still reports time_starttransfer under
# a millisecond behind such an upload, so the needle keeps no first-byte figure;
# the smoke's small request measures its first byte as the stream's first chunk.
ENGINE_CURL_SCRIPT: Final[str] = r'''set -eu
if [ -z "${1:-}" ] || [ ! -s "$1" ]; then
    echo "engine API key secret file is missing or empty" >&2
    exit 99
fi
umask 077
f=$(mktemp /tmp/gideon-engine-verify.XXXXXX) || exit 99
trap 'rm -f "$f"' EXIT
{
    printf 'header = "Authorization: Bearer '
    tr -d '\r\n' < "$1"
    printf '"\n'
} > "$f"
curl -K "$f" --silent --show-error --max-time "$3" -H 'Content-Type: application/json' -H 'Expect:' --data-binary @- -w '\n@gideon-engine-verify http_code=%{http_code} time_starttransfer=%{time_starttransfer} time_total=%{time_total}\n' "$2"
exit $?
'''

_MARKER: Final[str] = "@gideon-engine-verify"
_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{re.escape(_MARKER)} http_code=(?P<status>[0-9]+) "
    r"time_starttransfer=(?P<first>[0-9]+(?:\.[0-9]+)?) "
    r"time_total=(?P<elapsed>[0-9]+(?:\.[0-9]+)?)$",
    re.MULTILINE,
)
_MARKER_LINE_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{re.escape(_MARKER)}[^\r\n]*$", re.MULTILINE
)


@dataclass(frozen=True, slots=True)
class EngineReply:
    """The text-only result of one in-container curl request."""

    status: int | None
    body_text: str
    json: object | None
    events: tuple[Mapping[str, object], ...]
    done: bool
    time_to_first_byte: float | None
    elapsed: float | None
    problem: Problem | None

    def __repr__(self) -> str:
        """Keep response and stream text out of diagnostic representations."""

        return (
            "EngineReply("
            f"status={self.status!r}, body_text=<redacted>, json=<redacted>, "
            f"events={len(self.events)}, done={self.done!r}, "
            f"time_to_first_byte={self.time_to_first_byte!r}, elapsed={self.elapsed!r}, "
            f"problem={self.problem!r})"
        )


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One deterministic engine check and the figures it may keep."""

    name: str
    ok: bool
    detail: str
    fix: str
    figures: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EngineTarget:
    """The selected engine profile, served model, and context window."""

    profile_name: str
    served_model_name: str
    window: int


def _failed(name: str, detail: str, fix: str) -> StageResult:
    return StageResult(name, False, detail, fix)


def _failed_reply(problem: Problem) -> EngineReply:
    """A reply that never reached parsing: no status, no body, the problem alone."""

    return EngineReply(None, "", None, (), False, None, None, problem)


def _engine_fix(rendered_dir: PathLike) -> str:
    return (
        f"Do not go live on this engine. Run {stack.logs_fix(rendered_dir, ENGINE_SERVICE_NAME)}, "
        "revert the driver or engine change, then re-run sudo python3 -m gideon engine verify."
    )


def _frontend_fix(rendered_dir: PathLike) -> str:
    return (
        "Do not go live on this frontend. Run sudo python3 -m gideon apply "
        "(which pushes the guardrail Function and reads it back); "
        f"then read the frontend logs with {stack.logs_fix(rendered_dir, 'open-webui')}; "
        "then re-run sudo python3 -m gideon engine verify."
    )


def _rendered_has_engine(io: Host, rendered_dir: PathLike) -> bool | str:
    compose_path = Path(rendered_dir) / "compose.yaml"
    try:
        document = yaml.safe_load(io.read_text(compose_path))
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return "rendered Compose file is unavailable"
    except yaml.YAMLError:
        return "rendered Compose file is invalid"
    if not isinstance(document, Mapping):
        return "rendered Compose file has no services map"
    services = document.get("services")
    if not isinstance(services, Mapping):
        return "rendered Compose file has no services map"
    return ENGINE_SERVICE_NAME in services


def _response_problem(rendered_dir: PathLike, detail: str) -> Problem:
    return Problem(detail, _engine_fix(rendered_dir))


def _body_from_response(
    body_text: str, rendered_dir: PathLike
) -> tuple[object | None, tuple[Mapping[str, object], ...], bool, Problem | None]:
    lines = body_text.splitlines()
    is_sse = any(line.startswith("data:") for line in lines)
    if is_sse:
        events: list[Mapping[str, object]] = []
        done = False
        for line in lines:
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip()
            if payload == "[DONE]":
                done = True
                continue
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                return (
                    None,
                    (),
                    done,
                    _response_problem(rendered_dir, "engine response SSE payload is invalid"),
                )
            if not isinstance(parsed, Mapping):
                return (
                    None,
                    (),
                    done,
                    _response_problem(rendered_dir, "engine response SSE payload is not a mapping"),
                )
            events.append(parsed)
        return None, tuple(events), done, None

    if not body_text.strip():
        return None, (), False, None
    try:
        parsed_body = json.loads(body_text)
    except json.JSONDecodeError:
        return (
            None,
            (),
            False,
            _response_problem(rendered_dir, "engine response body is not valid JSON"),
        )
    return parsed_body, (), False, None


def _reply_from_output(
    stdout: str, rendered_dir: PathLike
) -> EngineReply:
    marker_lines = tuple(_MARKER_LINE_RE.finditer(stdout))
    if not marker_lines:
        return _failed_reply(_response_problem(rendered_dir, "engine response is missing its timing marker"))
    marker_line = marker_lines[-1]
    marker = _MARKER_RE.fullmatch(marker_line.group(0))
    if marker is None:
        return _failed_reply(_response_problem(rendered_dir, "engine response has an unparsable timing marker"))
    status = int(marker.group("status"))
    first_byte = float(marker.group("first"))
    elapsed = float(marker.group("elapsed"))
    body_text = stdout[: marker_line.start()].rstrip("\r\n")
    parsed_body, events, done, problem = _body_from_response(body_text, rendered_dir)
    return EngineReply(
        status,
        body_text,
        parsed_body,
        events,
        done,
        first_byte,
        elapsed,
        problem,
    )


def call_engine(
    io: Host,
    rendered_dir: PathLike,
    *,
    path: str,
    body: Mapping[str, object],
    max_time: int,
) -> EngineReply:
    """Call one engine endpoint from inside the engine's Compose network."""

    argv = stack.exec_argv(
        rendered_dir,
        ENGINE_SERVICE_NAME,
        "sh",
        "-c",
        ENGINE_CURL_SCRIPT,
        "gideon-engine-verify",
        f"/run/secrets/{ENGINE_SECRET_NAME}",
        f"http://{ENGINE_SERVICE_NAME}:{ENGINE_PORT}{path}",
        str(max_time),
    )
    try:
        result = io.run(
            argv,
            input=json.dumps(body),
            timeout=max_time + RUN_TIMEOUT_MARGIN_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _failed_reply(_response_problem(rendered_dir, "engine request failed: TimeoutExpired"))
    except OSError:
        return _failed_reply(_response_problem(rendered_dir, "engine request failed: OSError"))
    if result.returncode != 0:
        detail = f"engine request failed: non-zero exit {result.returncode}"
        diagnostic = _last_stderr_line(result.stderr)
        if diagnostic:
            detail += f" ({diagnostic})"
        return _failed_reply(_response_problem(rendered_dir, detail))
    return _reply_from_output(result.stdout, rendered_dir)


def _last_stderr_line(stderr: str) -> str:
    """curl's diagnostic, the last non-empty stderr line, bounded; never a body."""

    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1][:_STDERR_LIMIT] if lines else ""


def _error_message(reply: EngineReply) -> str:
    """The engine's own error text from a JSON error body, one bounded line."""

    if not isinstance(reply.json, Mapping):
        return ""
    error = reply.json.get("error")
    if not isinstance(error, Mapping):
        return ""
    message = error.get("message")
    return " ".join(str(message).split())[:_STDERR_LIMIT] if isinstance(message, str) else ""


def _status_failure(reply: EngineReply, engine_fix: str) -> tuple[str, str]:
    """A non-200 reply: a 400 is the request's fault and names the sample."""

    detail = f"engine returned HTTP {reply.status}"
    message = _error_message(reply)
    if message:
        detail += f": {message}"
    return detail, (_SAMPLE_FIX if reply.status == 400 else engine_fix)


def _tokenize(
    io: Host,
    rendered_dir: PathLike,
    *,
    served_name: str,
    messages: list[dict[str, str]],
    engine_fix: str,
) -> tuple[int | None, Problem | None]:
    """Return the engine's exact chat-template token count for *messages*."""

    body: Mapping[str, object] = {
        "model": served_name,
        "messages": messages,
        "add_generation_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    reply = call_engine(
        io,
        rendered_dir,
        path="/tokenize",
        body=body,
        max_time=TOKENIZE_TIMEOUT_SECONDS,
    )
    if reply.problem is not None:
        return None, reply.problem
    if reply.status != 200:
        detail, _ = _status_failure(reply, engine_fix)
        return None, Problem(detail, engine_fix)
    if not isinstance(reply.json, Mapping) or type(reply.json.get("count")) is not int:
        return None, Problem("tokenize response has no integer count", engine_fix)
    return reply.json["count"], None


def _needle_figures(
    *,
    nominal: int,
    target: int,
    floor: int,
    window: int,
    prompt_tokens: int | None,
    blocks: int,
    sizing_rounds: int,
    finish_reason: str | None = None,
    elapsed_seconds: float | None = None,
    completion_tokens: int | None = None,
    recalled: bool = False,
) -> dict[str, object]:
    return {
        "http_status": None,
        "nominal": nominal,
        "target": target,
        "floor": floor,
        "window": window,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "blocks": blocks,
        "sizing_rounds": sizing_rounds,
        "finish_reason": finish_reason,
        "elapsed_seconds": elapsed_seconds,
        "recalled": recalled,
    }


_BAND_DETAIL: Final[str] = "prompt could not be sized into its band"


def _needle_band(nominal: int, window: int) -> tuple[int, int]:
    """The target and floor for one nominal length: one formula for every length."""

    target = min(nominal, window - NEEDLE_MAX_TOKENS - NEEDLE_SIZING_MARGIN_TOKENS)
    return target, int(target * NEEDLE_MIN_FILL)


def _needle_name(nominal: int) -> str:
    return f"needle-{nominal // 1024}k"


@dataclass(frozen=True, slots=True)
class _Sizing:
    """The exact-sizing loop's result: the last candidate, its count, and the rounds."""

    messages: list[dict[str, str]]
    count: int | None
    blocks: int
    rounds: int
    problem: Problem | None


def _size_needle(
    io: Host,
    rendered_dir: PathLike,
    *,
    case: enginesample.NeedleCase,
    served_name: str,
    target: int,
    floor: int,
    per_block_tokens: int,
    overhead_tokens: int,
    engine_fix: str,
) -> _Sizing:
    """Tokenize whole candidates and correct the block count into ``[floor, target]``."""

    blocks = enginesample.blocks_for(target, per_block_tokens, overhead_tokens)
    messages: list[dict[str, str]] = []
    count: int | None = None
    candidate_blocks = blocks
    rounds = 0
    for rounds in range(1, NEEDLE_SIZING_ROUNDS + 1):
        candidate_blocks = blocks
        messages = enginesample.build_needle_prompt(case, blocks)
        count, problem = _tokenize(
            io, rendered_dir, served_name=served_name, messages=messages, engine_fix=engine_fix
        )
        if problem is not None:
            return _Sizing(messages, None, candidate_blocks, rounds, problem)
        assert count is not None
        if floor <= count <= target:
            break
        average = (count - overhead_tokens) / blocks
        if average <= 0:
            break
        difference = count - target if count > target else floor - count
        adjustment = max(1, math.ceil(difference / average))
        blocks = max(1, blocks - adjustment) if count > target else blocks + adjustment
    return _Sizing(messages, count, candidate_blocks, rounds, None)


def completion_values(
    reply: EngineReply,
) -> tuple[str | None, str | None, int | None, int | None]:
    """A non-streaming reply's content, finish reason, prompt tokens, and completion tokens."""

    content: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    if not isinstance(reply.json, Mapping):
        return content, finish_reason, prompt_tokens, completion_tokens
    choices = reply.json.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        choice = choices[0]
        if isinstance(choice.get("finish_reason"), str):
            finish_reason = choice["finish_reason"]
        message = choice.get("message")
        if isinstance(message, Mapping) and isinstance(message.get("content"), str):
            content = message["content"]
    usage = reply.json.get("usage")
    if isinstance(usage, Mapping):
        if type(usage.get("prompt_tokens")) is int:
            prompt_tokens = usage["prompt_tokens"]
        if type(usage.get("completion_tokens")) is int:
            completion_tokens = usage["completion_tokens"]
    return content, finish_reason, prompt_tokens, completion_tokens


def _normalised(text: str) -> str:
    return " ".join(text.split())


def _needle_check(
    io: Host,
    rendered_dir: PathLike,
    *,
    sample: enginesample.Sample,
    served_name: str,
    window: int,
    nominal: int,
    per_block_tokens: int,
    overhead_tokens: int,
    engine_fix: str,
) -> CheckOutcome:
    name = _needle_name(nominal)
    target, floor = _needle_band(nominal, window)
    figures = _needle_figures(
        nominal=nominal, target=target, floor=floor, window=window,
        prompt_tokens=None, blocks=0, sizing_rounds=0,
    )
    if floor < 1:
        return CheckOutcome(name, False, _BAND_DETAIL, _FILLER_FIX, figures)

    sizing = _size_needle(
        io,
        rendered_dir,
        case=sample.needle,
        served_name=served_name,
        target=target,
        floor=floor,
        per_block_tokens=per_block_tokens,
        overhead_tokens=overhead_tokens,
        engine_fix=engine_fix,
    )
    figures.update(prompt_tokens=sizing.count, blocks=sizing.blocks, sizing_rounds=sizing.rounds)
    if sizing.problem is not None:
        return CheckOutcome(name, False, sizing.problem.problem, engine_fix, figures)
    if sizing.count is None or not floor <= sizing.count <= target:
        return CheckOutcome(name, False, _BAND_DETAIL, _FILLER_FIX, figures)
    count = sizing.count

    request: Mapping[str, object] = {
        "model": served_name,
        "messages": sizing.messages,
        "max_tokens": NEEDLE_MAX_TOKENS,
        "temperature": 0,
        "seed": NEEDLE_SEED,
        "chat_template_kwargs": {"enable_thinking": False},
        "stream": False,
    }
    reply = call_engine(
        io, rendered_dir, path="/v1/chat/completions", body=request, max_time=NEEDLE_TIMEOUT_SECONDS
    )
    figures["http_status"] = reply.status
    if reply.problem is not None:
        return CheckOutcome(name, False, reply.problem.problem, engine_fix, figures)
    if reply.status != 200:
        detail, fix = _status_failure(reply, engine_fix)
        return CheckOutcome(name, False, detail, fix, figures)

    content, finish_reason, usage_prompt_tokens, completion_tokens = completion_values(reply)
    figures.update(
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        elapsed_seconds=reply.elapsed,
    )
    if content is None:
        return CheckOutcome(name, False, "engine reply has no string message content", engine_fix, figures)
    if usage_prompt_tokens != count:
        # The engine's template and its tokenizer route disagree: an engine anomaly, not the sample's.
        return CheckOutcome(
            name,
            False,
            f"tokenized prompt {count} tokens but usage.prompt_tokens was {usage_prompt_tokens}",
            engine_fix,
            figures,
        )
    recalled = _normalised(sample.needle.expected) in _normalised(content)
    figures["recalled"] = recalled
    verdict = "recalled" if recalled else "planted text not recalled"
    timing = f"answer in {reply.elapsed:.1f} s" if reply.elapsed is not None else "answer timing unavailable"
    detail = (
        f"{verdict}; prompt {count} tokens (target {target} of the {window} window; "
        f"{sizing.rounds} sizing rounds, {sizing.blocks} blocks), {timing}"
    )
    return CheckOutcome(name, recalled, detail, "" if recalled else engine_fix, figures)


def _needle_estimates(
    io: Host,
    rendered_dir: PathLike,
    *,
    sample: enginesample.Sample,
    served_name: str,
    engine_fix: str,
) -> tuple[int, int] | Problem:
    counts: list[int] = []
    for blocks in (1, 2):
        messages = enginesample.build_needle_prompt(sample.needle, blocks)
        count, problem = _tokenize(
            io,
            rendered_dir,
            served_name=served_name,
            messages=messages,
            engine_fix=engine_fix,
        )
        if problem is not None:
            return problem
        assert count is not None
        counts.append(count)
    per_block = counts[1] - counts[0]
    overhead = counts[0] - per_block
    if per_block <= 0 or overhead < 0:
        return Problem("needle sizing estimates are unusable", engine_fix)
    return per_block, overhead


def _structured_check(
    io: Host,
    rendered_dir: PathLike,
    *,
    sample: enginesample.Sample,
    served_name: str,
    engine_fix: str,
) -> CheckOutcome:
    """Run the deterministic JSON-schema response check."""

    case = sample.structured
    figures: dict[str, object] = {
        "http_status": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "finish_reason": None,
        "elapsed_seconds": None,
        "first_byte_seconds": None,
        "valid": False,
        "violations": 0,
    }
    body: Mapping[str, object] = {
        "model": served_name,
        "messages": [{"role": "user", "content": case.prompt}],
        "max_tokens": STRUCTURED_MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": case.schema_name,
                "schema": case.schema,
            },
        },
        "stream": False,
    }
    reply = call_engine(
        io,
        rendered_dir,
        path="/v1/chat/completions",
        body=body,
        max_time=STRUCTURED_TIMEOUT_SECONDS,
    )
    figures["http_status"] = reply.status
    if reply.problem is not None:
        return CheckOutcome("structured", False, reply.problem.problem, engine_fix, figures)
    if reply.status != 200:
        detail, fix = _status_failure(reply, engine_fix)
        return CheckOutcome("structured", False, detail, fix, figures)

    content, finish_reason, prompt_tokens, completion_tokens = completion_values(reply)
    figures.update(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
        elapsed_seconds=reply.elapsed,
        first_byte_seconds=reply.time_to_first_byte,
    )
    if content is None:
        return CheckOutcome(
            "structured",
            False,
            "engine reply has no string message content",
            engine_fix,
            figures,
        )
    if finish_reason != "stop":
        return CheckOutcome(
            "structured",
            False,
            f"answer did not finish (finish reason {finish_reason!r})",
            engine_fix,
            figures,
        )
    try:
        instance = json.loads(content)
    except json.JSONDecodeError:
        return CheckOutcome(
            "structured", False, "reply is not one JSON document", engine_fix, figures
        )
    violations = enginesample.validate(instance, case.schema)
    figures["violations"] = len(violations)
    if violations:
        return CheckOutcome(
            "structured",
            False,
            f"reply violates the schema at {', '.join(violations)}",
            engine_fix,
            figures,
        )
    figures["valid"] = True
    timing = f"answer in {reply.elapsed:.1f} s" if reply.elapsed is not None else "answer timing unavailable"
    tokens = str(completion_tokens) if completion_tokens is not None else "unknown"
    return CheckOutcome(
        "structured",
        True,
        f"valid against {case.schema_name}; {tokens} completion tokens, {timing}",
        "",
        figures,
    )


def _stream_values(
    reply: EngineReply,
) -> tuple[str, int | None, int | None, str | None]:
    text_parts: list[str] = []
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    for event in reply.events:
        choices = event.get("choices")
        if choices == []:
            usage = event.get("usage")
            if isinstance(usage, Mapping):
                prompt = usage.get("prompt_tokens")
                completion = usage.get("completion_tokens")
                if type(prompt) is int:
                    prompt_tokens = prompt
                if type(completion) is int:
                    completion_tokens = completion
            continue
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, Mapping):
            continue
        candidate_finish = choice.get("finish_reason")
        if isinstance(candidate_finish, str):
            finish_reason = candidate_finish
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            continue
        for key in ("reasoning", "content"):
            value = delta.get(key)
            if isinstance(value, str):
                text_parts.append(value)
    return "".join(text_parts), prompt_tokens, completion_tokens, finish_reason


def _smoke_check(
    io: Host,
    rendered_dir: PathLike,
    *,
    sample: enginesample.Sample,
    served_name: str,
    engine_fix: str,
) -> CheckOutcome:
    body: Mapping[str, object] = {
        "model": served_name,
        "messages": [
            {"role": "user", "content": sample.smoke.prompt},
        ],
        "max_tokens": sample.smoke.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    reply = call_engine(
        io,
        rendered_dir,
        path="/v1/chat/completions",
        body=body,
        max_time=SMOKE_TIMEOUT_SECONDS,
    )
    figures: dict[str, object] = {"http_status": reply.status}
    if reply.problem is not None:
        return CheckOutcome("smoke", False, reply.problem.problem, engine_fix, figures)
    if reply.status != 200:
        detail, fix = _status_failure(reply, engine_fix)
        return CheckOutcome("smoke", False, detail, fix, figures)

    text, prompt_tokens, completion_tokens, finish_reason = _stream_values(reply)
    if reply.elapsed is None or reply.time_to_first_byte is None:
        return CheckOutcome("smoke", False, "engine response has no timing figures", engine_fix, figures)

    chars = len(text)
    interval = reply.elapsed - reply.time_to_first_byte
    chars_per_second = chars / interval if interval > 0 else 0.0
    tokens_per_second = (
        completion_tokens / interval
        if interval > 0 and completion_tokens is not None
        else 0.0
    )
    figures.update(
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        chars=chars,
        elapsed_seconds=reply.elapsed,
        first_byte_seconds=reply.time_to_first_byte,
        chars_per_second=chars_per_second,
        tokens_per_second=tokens_per_second,
    )

    if interval <= 0:
        return CheckOutcome(
            "smoke",
            False,
            "stream timing interval is zero or negative",
            engine_fix,
            figures,
        )
    if prompt_tokens is None or completion_tokens is None:
        return CheckOutcome(
            "smoke",
            False,
            "stream usage chunk is missing token figures",
            engine_fix,
            figures,
        )
    implied = {
        "lag": guardrail.LAG_CHARS / chars_per_second if chars_per_second > 0 else 0.0,
        "restatement": 2 * guardrail.LAG_CHARS / chars_per_second if chars_per_second > 0 else 0.0,
    }
    figures["implied_lag_seconds"] = implied

    faults: list[str] = []
    if not reply.done:
        faults.append("no [DONE] terminator")
    if not text:
        faults.append("empty text")
    if finish_reason not in ("stop", "length"):
        faults.append(f"finish reason {finish_reason!r}")
    detail = (
        f"single stream {chars_per_second:.1f} chars/s ({tokens_per_second:.1f} tok/s), "
        f"first byte {reply.time_to_first_byte:.3f} s, {reply.elapsed:.1f} s total; "
        f"implied lag {implied['lag']:.1f} s, restatement {implied['restatement']:.1f} s"
    )
    if faults:
        detail = "stream did not complete: " + ", ".join(faults)
    return CheckOutcome("smoke", not faults, detail, engine_fix if faults else "", figures)


def _frontend_outcome(
    stored: owuiturn.StoredTurn, prompt: str
) -> tuple[str | None, str | None]:
    """Classify one stored frontend answer with the package's guardrail judge."""

    if stored.problem is not None:
        return None, None
    assistant = stored.assistant
    if not isinstance(assistant, Mapping):
        return None, None
    content = assistant.get("content")
    if not isinstance(content, str) or not content.strip():
        return None, None

    trip = guardrail.judge_message(assistant, [stored.user or {}, assistant], 1)
    if trip is not None:
        return "leak", trip.pattern_id

    prompt_dates = guardrail.normalized_dates(prompt)
    stored_dates: set[str] = set()
    for text in guardrail.rendered_texts(assistant):
        stored_dates.update(guardrail.normalized_dates(text))
    if not stored_dates.issubset(prompt_dates):
        return "computed", None

    refusals = tuple(_normalised(refusal) for refusal in guardrail.REFUSALS)
    if any(_normalised(content).endswith(refusal) for refusal in refusals):
        # The service stores one shape: the released prefix, the module's
        # separator, then the refusal; there is no pre-outlet answer to read.
        return "refused-stream", None
    return "answered", None


def _frontend_figures(role: str) -> dict[str, object]:
    return {
        "role": role,
        "elapsed_seconds": None,
        "chats_created": 0,
        "stored": False,
        "finished": False,
        "outcome": None,
        "leak_pattern": None,
        "chat_deleted": False,
    }


def _frontend_failure(
    name: str, figures: Mapping[str, object], detail: str, fix: str
) -> CheckOutcome:
    return CheckOutcome(name, False, detail, fix, figures)


def _frontend_verdict(
    role: str, outcome: str, elapsed_seconds: float
) -> tuple[bool, str]:
    if role == "positive":
        if outcome == "refused-stream":
            return True, f"refused (in the stream), turn {elapsed_seconds:.1f} s"
        if outcome == "answered":
            return True, f"no computation stored (the model's own answer), turn {elapsed_seconds:.1f} s"
        if outcome == "leak":
            return False, "a matched span was stored"
        if outcome == "computed":
            # A calendar date the prompt did not carry, in the answer alone, with no
            # deadline vocabulary near it: the family's detector needs that context by
            # design (§16), so this is the model's behaviour under General's instructed
            # refusal, recorded for the eval and the family tickets, never a fault an
            # install or upgrade could correct. The reasoning is withheld and never
            # stored since slice-1 ticket 37.
            return True, (
                "no matched span stored; a new calendar date recorded "
                f"(the family's context rule), turn {elapsed_seconds:.1f} s"
            )
    else:
        if outcome == "refused-stream":
            return True, f"refused (in the stream), turn {elapsed_seconds:.1f} s"
        if outcome in {"answered", "computed"}:
            return False, "the trip stimulus was not refused"
        if outcome == "leak":
            return False, "a matched span was stored"
    return False, "frontend outcome is unknown"


def _frontend_case(
    client: owui.Client,
    case: enginesample.FrontendCase,
    *,
    role: str,
    run_token: str,
    frontend_fix: str,
) -> CheckOutcome:
    name = f"{FRONTEND_ROW_PREFIX}{case.id}"
    figures = _frontend_figures(role)
    result: CheckOutcome | None = None
    turn_chat_id: str | None = None
    try:
        before = owuiturn.chat_ids(client)
        user_id = str(uuid.uuid4())
        assistant_id = str(uuid.uuid4())
        prompt = f"{case.prompt}\n\n[engine verify {run_token} {case.id}]"
        started = time.monotonic()
        turn_problem = owuiturn.managed_turn(
            client,
            model=GENERAL_PRESET_ID,
            prompt=prompt,
            user_id=user_id,
            assistant_id=assistant_id,
            timestamp=int(time.time()),
        )
        elapsed = time.monotonic() - started
        figures["elapsed_seconds"] = elapsed
        found = owuiturn.find_turn_chat(
            client,
            ids_before=before,
            user_id=user_id,
            assistant_id=assistant_id,
        )
        figures["chats_created"] = found.candidates
        turn_chat_id = found.chat_id
        stored = found.stored
        if turn_problem is not None:
            result = _frontend_failure(name, figures, turn_problem.problem, frontend_fix)
        elif stored is None:
            result = _frontend_failure(
                name, figures, found.refusal().problem, frontend_fix
            )
        else:
            figures["stored"] = True
            figures["finished"] = stored.problem is None
            outcome, pattern = _frontend_outcome(stored, prompt)
            figures["outcome"] = outcome
            figures["leak_pattern"] = pattern
            if outcome is None:
                detail = (
                    stored.problem.problem if stored.problem is not None else "answer is empty"
                )
                result = _frontend_failure(name, figures, detail, frontend_fix)
            else:
                ok, detail = _frontend_verdict(role, outcome, elapsed)
                result = CheckOutcome(name, ok, detail, "" if ok else frontend_fix, figures)
    except owui.OwuiError as exc:
        result = _frontend_failure(name, figures, exc.problem, frontend_fix)
    finally:
        if turn_chat_id is not None:
            deletion_problem: str | None = None
            try:
                deletion = owuiturn.delete_chat(client, turn_chat_id)
            except owui.OwuiError as exc:
                deletion_problem = exc.problem
            else:
                if deletion is not None:
                    deletion_problem = deletion.problem
            if deletion_problem is None:
                figures["chat_deleted"] = True
            else:
                prior = result.detail if result is not None else "frontend turn failed"
                result = _frontend_failure(
                    name,
                    figures,
                    f"{prior}; chat deletion failed: {deletion_problem}",
                    _FRONTEND_DELETE_FIX,
                )
    if result is None:
        result = _frontend_failure(name, figures, "frontend turn failed", frontend_fix)
    return result


def _frontend_checks(
    client_factory: Callable[..., owui.Client],
    *,
    sample: enginesample.Sample,
    password: str,
    run_token: str,
    frontend_fix: str,
) -> Iterator[CheckOutcome]:
    """Yield one outcome per frontend case as it completes: the positives, then the trip.

    One sign-in serves every case; when it fails, every case fails with the
    frontend fix and nothing is sent.
    """

    cases = tuple(
        [("positive", case) for case in sample.frontend.positives]
        + [("trip", sample.frontend.trip)]
    )
    try:
        token = owuiturn.signin(client_factory, EVAL_IDENTITY.email, password)
        client = client_factory(token=token)
    except owui.OwuiError as exc:
        for role, case in cases:
            yield _frontend_failure(
                f"{FRONTEND_ROW_PREFIX}{case.id}",
                _frontend_figures(role),
                f"frontend sign-in failed: {exc.problem}",
                frontend_fix,
            )
        return
    for role, case in cases:
        yield _frontend_case(
            client,
            case,
            role=role,
            run_token=run_token,
            frontend_fix=frontend_fix,
        )


def _audit_detail(
    outcome: str,
    profile: str,
    served_name: str,
    window: int,
    sample_sha256: str,
    checks: tuple[CheckOutcome, ...],
) -> Mapping[str, object]:
    return {
        "outcome": outcome,
        "profile": profile,
        "served_model": served_name,
        "max_model_len": window,
        "sample_sha256": sample_sha256,
        "checks": {
            check.name: {"ok": check.ok, **dict(check.figures)} for check in checks
        },
    }


def resolve_engine_target(
    io: Host,
    rendered_dir: PathLike,
    *,
    hardware_profile: str,
    models_path: PathLike,
    sleep: Callable[[float], None],
) -> EngineTarget | Problem:
    """Resolve the rendered, pinned, and currently healthy engine target."""

    rendered = _rendered_has_engine(io, rendered_dir)
    if rendered is not True:
        detail = rendered if isinstance(rendered, str) else "rendered Compose has no engine service"
        return Problem(detail, _APPLY_FIX)

    models_result = models.load_models_lock(models_path, host=io)
    if models_result.errors or models_result.lock is None:
        fix = models_result.errors[0].fix if models_result.errors else _MODELS_FIX
        return Problem(models.render_errors(models_result.errors), fix)
    selected = models.select_profile(models_result.lock, hardware_profile)
    if isinstance(selected, Problem):
        return selected
    generator = selected.model("generator")
    if generator is None:
        return Problem(
            f"models.lock profile '{selected.name}' has no generator pin",
            _MODELS_FIX,
        )
    window = generator.serve.flags.get("max-model-len")
    if type(window) is not int or window <= 0:
        return Problem(
            "models.lock generator pin has no usable max-model-len flag",
            _MODELS_FIX,
        )

    ready, detail, service = apply.wait_for_services(
        io,
        rendered_dir,
        (ENGINE_SERVICE_NAME,),
        sleep,
        exact=False,
        require_healthy=True,
        attempts=1,
    )
    if not ready:
        return Problem(detail, stack.logs_fix(rendered_dir, service))
    return EngineTarget(selected.name, generator.serve.served_name, window)


def run_engine_verify(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    site_path: PathLike = _SITE_PATH,
    rendered_dir: PathLike = _RENDERED_DIR,
    root: PathLike | None = None,
    models_path: PathLike | None = None,
    sample_path: PathLike | None = None,
    audit: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    client_factory: Callable[..., owui.Client] | None = None,
    observe: Callable[[StageResult], None] | None = None,
) -> int:
    """Run the ordered direct engine verification checks."""

    del args
    io = host or RealHost()
    audit_api = audit if audit is not None else audit_module

    def show(result: StageResult) -> None:
        print_stage(result)
        if observe is not None:
            observe(result)

    checkout = Path(__file__).parents[2] if root is None else Path(root)
    actual_models = checkout / "models.lock" if models_path is None else models_path
    actual_sample = (
        checkout / "eval/engine-verify/sample.yaml"
        if sample_path is None
        else sample_path
    )

    if io.geteuid() != 0:
        show(_failed("preconditions", "root privileges are required.", _ROOT_FIX))
        return 1
    if nogpu.is_no_gpu_host(io):
        show(StageResult("engine", True, "skipped — no-GPU host (§2.5)", ""))
        return 0

    site_result = site.load_site(Path(site_path), host=io)
    if site_result.errors or site_result.config is None:
        show(
            _failed(
                "preconditions",
                site.render_errors(site_result.errors),
                "Correct the site file, then retry",
            )
        )
        return 1
    config = site_result.config

    engine_target = resolve_engine_target(
        io,
        rendered_dir,
        hardware_profile=config.hardware_profile,
        models_path=actual_models,
        sleep=sleep,
    )
    if isinstance(engine_target, Problem):
        show(_failed("preconditions", engine_target.problem, engine_target.fix))
        return 1

    sample_result = enginesample.load_sample(actual_sample, host=io)
    if sample_result.errors or sample_result.sample is None:
        fix = sample_result.errors[0].fix if sample_result.errors else _SAMPLE_FIX
        show(_failed("preconditions", enginesample.render_errors(sample_result.errors), fix))
        return 1
    sample = sample_result.sample

    password_result = secrets.read_secret(io, EVAL_PASSWORD_SECRET)
    if not password_result.ok or password_result.value is None:
        password_fix = _APPLY_FIX if password_result.missing else password_result.fix
        show(
            _failed(
                "preconditions",
                password_result.problem or "eval password is unavailable",
                password_fix or _APPLY_FIX,
            )
        )
        return 1
    eval_password = password_result.value

    try:
        audit_problem = audit_api.probe(io, rendered_dir)
    except OSError:
        audit_problem = "audit writer probe failed"
    if audit_problem is not None:
        show(_failed("preconditions", "audit writer is unavailable", _APPLY_FIX))
        return 1

    show(
        StageResult(
            "preconditions",
            True,
            f"root, site, rendered engine, profile {engine_target.profile_name}, window {engine_target.window}, "
            f"sample {sample.sha256[:12]}, eval password, and audit writer are ready",
            "",
        )
    )

    engine_fix = _engine_fix(rendered_dir)
    served_name = engine_target.served_model_name
    window = engine_target.window
    checks: list[CheckOutcome] = []

    def record(outcome: CheckOutcome) -> None:
        """Print a check's row as it completes, so a long run streams its rows."""

        show(StageResult(outcome.name, outcome.ok, outcome.detail, outcome.fix))
        checks.append(outcome)

    estimates = _needle_estimates(
        io, rendered_dir, sample=sample, served_name=served_name, engine_fix=engine_fix
    )
    for nominal in NEEDLE_LENGTHS:
        if isinstance(estimates, Problem):
            target, floor = _needle_band(nominal, window)
            figures = _needle_figures(
                nominal=nominal, target=target, floor=floor, window=window,
                prompt_tokens=None, blocks=0, sizing_rounds=0,
            )
            record(CheckOutcome(_needle_name(nominal), False, estimates.problem, engine_fix, figures))
            continue
        per_block_tokens, overhead_tokens = estimates
        record(
            _needle_check(
                io,
                rendered_dir,
                sample=sample,
                served_name=served_name,
                window=window,
                nominal=nominal,
                per_block_tokens=per_block_tokens,
                overhead_tokens=overhead_tokens,
                engine_fix=engine_fix,
            )
        )
    record(
        _structured_check(
            io,
            rendered_dir,
            sample=sample,
            served_name=served_name,
            engine_fix=engine_fix,
        )
    )
    record(
        _smoke_check(
            io,
            rendered_dir,
            sample=sample,
            served_name=served_name,
            engine_fix=engine_fix,
        )
    )

    frontend_factory = client_factory
    if frontend_factory is None:
        frontend_factory = owui.ingress_client_factory(
            config.hostname,
            ca_path=tls.CA_PATH,
            timeout=FRONTEND_TURN_TIMEOUT_SECONDS,
        )
    run_id = uuid.uuid4()
    for outcome in _frontend_checks(
        frontend_factory,
        sample=sample,
        password=eval_password,
        run_token=run_id.hex[:8],
        frontend_fix=_frontend_fix(rendered_dir),
    ):
        record(outcome)

    row = audit_module.AuditRow(
        run_id=str(run_id),
        kind="engine_verify",
        actor_user_id=None,
        user_id=None,
        chat_id=None,
        kb_ids=(),
        detail=_audit_detail(
            "ok" if all(check.ok for check in checks) else "failed",
            engine_target.profile_name,
            served_name,
            window,
            sample.sha256,
            tuple(checks),
        ),
    )
    try:
        audit_problem = audit_api.write_rows(io, rendered_dir, (row,))
    except OSError:
        audit_problem = "audit writer failed"
    if audit_problem is None:
        audit_result = StageResult("audit", True, "engine_verify audit row recorded", "")
    else:
        audit_result = _failed(
            "audit",
            "engine_verify audit write failed",
            stack.logs_fix(rendered_dir, "postgres"),
        )
    show(audit_result)
    return int(not (all(check.ok for check in checks) and audit_result.ok))
