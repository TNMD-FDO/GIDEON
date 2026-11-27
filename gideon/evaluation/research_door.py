"""Send retrieval and full turns or probe Research through the eval identity's API door.

``send`` reads the retrieval reply and ``send_full`` reads the named event stream.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from sys import float_info
from types import MappingProxyType
from typing import Final, Literal, cast, get_args

from gideon.evaluation.turns import door
from gideon.host import report, stack
from gideon.host.render.api import API_SECRET_NAME, API_SERVICE_NAME
from gideon.host.render.engine import ENGINE_PORT
from gideon.host.render.worker import GENERATION_PATTERN
from gideon.host.report import Problem, Timeout
from gideon.host.staging import DIGEST_PATTERN, LABEL_PATTERN
from gideon.host.sysio import Host, PathLike
from gideon.turn import events
from gideon.turn.states import State

TURN_PATH: Final = "/turn"
# exempt: a starting bound, corrected by the M1 reading.
TURN_TIMEOUT_SECONDS: Final = 60.0
# exempt: a starting bound, corrected by the M5 reading.
FULL_TURN_TIMEOUT_SECONDS: Final = 900.0
# An override key no release configuration holds, so the turn refuses it at admission,
# before it reads the record or a store.
PROBE_KEY: Final = "eval_probe"
PROBE_MESSAGE: Final = "Research availability probe."
SIBLING_FIX_COMMAND: Final = "sudo python3 -m tools.cistack up"
_EVENT_KINDS: Final = get_args(events.EventKind)
_END_WORDS: Final = get_args(events.EndWord)


@dataclass(frozen=True, slots=True)
class Reply:
    """The checked generation, configuration, ordered chunk ids, and total milliseconds."""

    generation_id: str
    lockfiles: tuple[str, ...]
    config_hash: str
    chunk_ids: tuple[str, ...]
    latency_ms: float


@dataclass(frozen=True, slots=True)
class FullReply:
    """The checked facts and terminal answer of a streamed turn."""

    retrieval: Reply
    evidence: tuple[str, ...]
    answer: str = field(repr=False)
    end: events.EndWord
    family: str | None = field(repr=False)
    attempts: int
    text_events: int
    timings_ms: Mapping[str, float | None] = field(repr=False)
    latency_ms: float
    elapsed: float | None


@dataclass(frozen=True, slots=True)
class Failure:
    """A fixed failure code with the turn's state when an HTTP refusal was read."""

    code: Literal["turn-failed", "turn-cut", "turn-reply-invalid"]
    state: State | Literal["unknown"] | None = None


def _retrieve(
    io: Host, rendered_dir: PathLike, message: str, overrides: Mapping[str, object] | None,
) -> door.DoorReply:
    body: dict[str, object] = {"message": message, "stop_after": "retrieve"}
    if overrides:
        body["overrides"] = dict(overrides)
    return door.request(
        io, rendered_dir, url=f"http://{API_SERVICE_NAME}:{ENGINE_PORT}{TURN_PATH}",
        body=body, max_time=TURN_TIMEOUT_SECONDS, keep_failure_detail=True,
    )


def _read_object(text: str) -> dict[str, object]:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _state(text: str) -> State | Literal["unknown"]:
    error = _read_object(text).get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str):
        try:
            return State(code)
        except ValueError:
            pass
    return "unknown"


def _chunk_ids(pool: object) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if not isinstance(pool, list):
        return None
    by_rank: dict[int, str] = {}
    by_pool_rank: dict[int, str] = {}
    seen: set[str] = set()
    for entry in pool:
        if not isinstance(entry, dict):
            return None
        rank, pool_rank, chunk_id = entry.get("rank"), entry.get("pool_rank"), entry.get("chunk_id")
        if (
            type(rank) is not int or not 1 <= rank <= len(pool) or rank in by_rank
            or type(pool_rank) is not int or not 1 <= pool_rank <= len(pool) or pool_rank in by_pool_rank
            or not isinstance(chunk_id, str) or DIGEST_PATTERN.fullmatch(chunk_id) is None
            or chunk_id in seen
        ):
            return None
        by_rank[rank] = chunk_id
        by_pool_rank[pool_rank] = chunk_id
        seen.add(chunk_id)
    ranks = range(1, len(pool) + 1)
    return tuple(by_rank[rank] for rank in ranks), tuple(by_pool_rank[rank] for rank in ranks)


def _parse_reply(body: str | dict[str, object]) -> tuple[Reply, tuple[str, ...]] | Failure:
    data = _read_object(body) if isinstance(body, str) else body
    generation = data.get("generation")
    timings = data.get("timings_ms")
    if data.get("stage") != "retrieve" or not isinstance(generation, dict) or not isinstance(timings, dict):
        return Failure("turn-reply-invalid")
    gen_id, lockfiles = generation.get("gen_id"), generation.get("lockfiles")
    digest, total = data.get("retrieval_config_hash"), timings.get("total")
    if (
        not isinstance(gen_id, str) or re.fullmatch(GENERATION_PATTERN, gen_id) is None
        or not isinstance(lockfiles, list) or not lockfiles
        or not all(isinstance(label, str) and LABEL_PATTERN.fullmatch(label) for label in lockfiles)
        or not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None
        or (type(total) is not int and type(total) is not float) or not 0 <= total <= float_info.max
    ):
        return Failure("turn-reply-invalid")
    chunk_ids = _chunk_ids(data.get("pool"))
    if chunk_ids is None:
        return Failure("turn-reply-invalid")
    ranked, fused = chunk_ids
    return Reply(gen_id, tuple(lockfiles), digest, ranked, float(total)), fused


def _parse_answer(
    data: dict[str, object], retrieval: Reply, evidence: tuple[str, ...], attempts: int,
    text_events: int, elapsed: float | None,
) -> FullReply | Failure:
    answer, end = data.get("text"), data.get("end")
    raw_timings = data.get("timings_ms")
    if not isinstance(answer, str) or end not in _END_WORDS or not isinstance(raw_timings, dict):
        return Failure("turn-reply-invalid")
    family: str | None = None
    if end == "tripped":
        raw_family = data.get("family")
        if not isinstance(raw_family, str):
            return Failure("turn-reply-invalid")
        family = raw_family
    elif "family" in data:
        return Failure("turn-reply-invalid")

    total = raw_timings.get("total")
    if (type(total) is not int and type(total) is not float) or not 0 <= total <= float_info.max:
        return Failure("turn-reply-invalid")
    for value in raw_timings.values():
        if value is not None and type(value) is not int and type(value) is not float:
            return Failure("turn-reply-invalid")

    return FullReply(
        retrieval, evidence, answer, cast(events.EndWord, end), family,
        attempts, text_events, MappingProxyType(raw_timings), float(total), elapsed,
    )


def _parse_full_reply(reply: door.DoorReply) -> FullReply | Failure:
    passages: list[tuple[int, str]] = []
    retrieval: Reply | None = None
    attempts = 0
    text_events = 0
    for position, record in enumerate(reply.named_events, start=1):
        if record.name not in _EVENT_KINDS:
            return Failure("turn-reply-invalid")
        data = _read_object(record.payload)
        stage = data.get("stage")
        match record.name:
            case "passage" if retrieval is None:
                number, rank, chunk_id = data.get("number"), data.get("pool_rank"), data.get("chunk_id")
                if (
                    type(number) is not int or number != len(passages) + 1
                    or type(rank) is not int or rank < 1 or not isinstance(chunk_id, str)
                ):
                    return Failure("turn-reply-invalid")
                passages.append((rank, chunk_id))
            case "phase" if stage == "retrieve" and retrieval is None:
                body = data.get("retrieval")
                if not isinstance(body, dict):
                    return Failure("turn-reply-invalid")
                parsed = _parse_reply(body)
                if isinstance(parsed, Failure):
                    return parsed
                retrieval, fused = parsed
                # Passage events carry fused ranks.
                if any(
                    rank > len(fused) or fused[rank - 1] != chunk_id
                    for rank, chunk_id in passages
                ):
                    return Failure("turn-reply-invalid")
            case "phase" if stage == "generate" and retrieval is not None:
                attempt = data.get("attempt")
                if type(attempt) is not int or attempt != attempts + 1 or attempt > 2:
                    return Failure("turn-reply-invalid")
                attempts = attempt
            case "text" if attempts and isinstance(data.get("text"), str):
                text_events += 1
            # The service's catch-all can fail the turn before its phases are sent.
            case "failure" if position == len(reply.named_events) and isinstance(data.get("error"), dict):
                return Failure("turn-failed", _state(record.payload))
            case "answer" if retrieval is not None and attempts and position == len(reply.named_events):
                evidence = tuple(chunk_id for _, chunk_id in passages)
                return _parse_answer(data, retrieval, evidence, attempts, text_events, reply.elapsed)
            case _:
                return Failure("turn-reply-invalid")
    return Failure("turn-reply-invalid")


def _refusal(reply: door.DoorReply) -> Failure | None:
    """Map a cut, an HTTP refusal, or a door problem to its fixed failure."""

    if isinstance(reply.problem, Timeout):
        return Failure("turn-cut")
    if reply.status is not None and reply.status != HTTPStatus.OK:
        return Failure("turn-failed", _state(reply.body_text))
    if reply.status != HTTPStatus.OK or reply.problem is not None:
        return Failure("turn-failed")
    return None


def send(
    io: Host, rendered_dir: PathLike, message: str, overrides: Mapping[str, object] | None = None,
) -> Reply | Failure:
    """Run one retrieval turn and retain only checked facts or a fixed failure."""

    reply = _retrieve(io, rendered_dir, message, overrides)
    refused = _refusal(reply)
    if refused is not None:
        return refused
    parsed = _parse_reply(reply.body_text)
    return parsed if isinstance(parsed, Failure) else parsed[0]


def send_full(
    io: Host, rendered_dir: PathLike, message: str, overrides: Mapping[str, object] | None = None,
    *, message_id: str | None = None,
) -> FullReply | Failure:
    """Run a full Research turn and return its checked terminal outcome."""

    body: dict[str, object] = {"message": message}
    if message_id is not None:
        body["message_id"] = message_id
    if overrides:
        body["overrides"] = dict(overrides)
    reply = door.request(
        io, rendered_dir, url=f"http://{API_SERVICE_NAME}:{ENGINE_PORT}{TURN_PATH}",
        body=body, max_time=FULL_TURN_TIMEOUT_SECONDS, keep_failure_detail=True, named_events=True,
    )
    refused = _refusal(reply)
    if refused is not None:
        return refused
    if not reply.is_stream:
        return Failure("turn-reply-invalid")
    return _parse_full_reply(reply)


def probe(io: Host, rendered_dir: PathLike, *, sibling: bool = False) -> Problem | None:
    """Require an override refusal before the turn can read the record or stores.

    The CI sibling keeps its own rendered tree and API key, so its refusals name
    the sibling's re-render rather than production's upgrade, rotation, or apply.
    """

    reply = _retrieve(io, rendered_dir, PROBE_MESSAGE, {PROBE_KEY: True})
    if reply.status == HTTPStatus.BAD_REQUEST and _state(reply.body_text) is State.OVERRIDE_NOT_ALLOWED:
        return None
    if sibling and reply.status in (HTTPStatus.NOT_FOUND, HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return Problem(
            f"the CI sibling's Research probe was refused (HTTP {reply.status})",
            f"Run {SIBLING_FIX_COMMAND} from this checkout to re-render the sibling, then retry.",
        )
    if reply.status == HTTPStatus.NOT_FOUND:
        return Problem(
            "the API service does not serve /turn",
            f"Run {report.command('upgrade <tag>')} with a release that serves /turn, then retry.",
        )
    if reply.status == HTTPStatus.UNAUTHORIZED:
        return Problem(
            "the Research probe's API key was refused",
            f"Run {report.command(f'secrets rotate {API_SECRET_NAME}')}, then retry.",
        )
    if reply.status == HTTPStatus.FORBIDDEN:
        return Problem(
            "the Research probe's eval identity was refused",
            f"Run {report.command('render --diff')} to compare the API service's eval identity with "
            f"this checkout's, run {report.command('apply')} when they differ, then retry.",
        )
    return Problem(
        "the Research probe did not receive the expected override refusal",
        f"Run {stack.logs_fix(rendered_dir, API_SERVICE_NAME)}, then retry.",
    )
