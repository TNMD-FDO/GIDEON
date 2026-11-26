"""Send retrieval requests and probe Research through the eval identity's API door."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from sys import float_info
from typing import Final, Literal

from gideon.evaluation.turns import door
from gideon.host import report, stack
from gideon.host.render.api import API_SECRET_NAME, API_SERVICE_NAME
from gideon.host.render.engine import ENGINE_PORT
from gideon.host.render.worker import GENERATION_PATTERN
from gideon.host.report import Problem, Timeout
from gideon.host.staging import DIGEST_PATTERN, LABEL_PATTERN
from gideon.host.sysio import Host, PathLike
from gideon.turn.states import State

TURN_PATH: Final = "/turn"
# exempt: a starting bound, corrected by the M1 reading.
TURN_TIMEOUT_SECONDS: Final = 60.0
# An override key no release configuration holds, so the turn refuses it at admission,
# before it reads the record or a store.
PROBE_KEY: Final = "eval_probe"
PROBE_MESSAGE: Final = "Research availability probe."
SIBLING_FIX_COMMAND: Final = "sudo python3 -m tools.cistack up"


@dataclass(frozen=True, slots=True)
class Reply:
    """The checked generation, configuration, ordered chunk ids, and total milliseconds."""

    generation_id: str
    lockfiles: tuple[str, ...]
    config_hash: str
    chunk_ids: tuple[str, ...]
    latency_ms: float


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


def _chunk_ids(pool: object) -> tuple[str, ...] | None:
    if not isinstance(pool, list):
        return None
    by_rank: dict[int, str] = {}
    seen: set[str] = set()
    for entry in pool:
        if not isinstance(entry, dict):
            return None
        rank, chunk_id = entry.get("pool_rank"), entry.get("chunk_id")
        if (
            type(rank) is not int or not 1 <= rank <= len(pool) or rank in by_rank
            or not isinstance(chunk_id, str) or DIGEST_PATTERN.fullmatch(chunk_id) is None
            or chunk_id in seen
        ):
            return None
        by_rank[rank] = chunk_id
        seen.add(chunk_id)
    return tuple(by_rank[rank] for rank in range(1, len(pool) + 1))


def _parse_reply(text: str) -> Reply | Failure:
    data = _read_object(text)
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
    return Reply(gen_id, tuple(lockfiles), digest, chunk_ids, float(total))


def send(
    io: Host, rendered_dir: PathLike, message: str, overrides: Mapping[str, object] | None = None,
) -> Reply | Failure:
    """Run one retrieval turn and retain only checked facts or a fixed failure."""

    reply = _retrieve(io, rendered_dir, message, overrides)
    if isinstance(reply.problem, Timeout):
        return Failure("turn-cut")
    if reply.status is not None and reply.status != HTTPStatus.OK:
        return Failure("turn-failed", _state(reply.body_text))
    if reply.status != HTTPStatus.OK or reply.problem is not None:
        return Failure("turn-failed")
    return _parse_reply(reply.body_text)


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
