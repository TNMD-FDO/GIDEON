"""Run each case as a managed turn, record it, and remove its chats."""

import concurrent.futures
import json
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from gideon.host import owui, owuiturn
from gideon.host.owui import Client
from gideon.host.render.owui import EVAL_IDENTITY, GENERAL_PRESET_ID
from gideon.host.report import Problem, StageResult, print_stage
from gideon.host.sysio import Host, RealHost
from tools.ownership import restore_ownership, sudo_ids
from tools.turns import browser, chromium, classify, session
from tools.turns.cases import CASE_KINDS, Case

# A managed turn waits for the frontend to finish the engine, outlet, and
# persistence; the starting bound covers the observed longest turn.
TURN_TIMEOUT_SECONDS: Final[float] = 600.0
# §18.5: a handful of engine calls may run at any hour, a longer run only in the
# quiet window or on a weekend; this is the handful — a starting value. A
# browser turn counts BROWSER_ENGINE_CALLS_PER_TURN engine calls.
SMOKE_TURNS: Final[int] = 8
# A frontend page's turn sends the answer, then the page's title and tag tasks
# (follow-ups are rendered off in render/owui.py); the API mode's managed turn
# and the probe's chat-id completion send no background tasks and cost one. The
# starting value comes from ticket 36's proof: 65 engine calls for 24
# completions, or 62 over 21 browser turns after the three one-call probes.
# Correct it from the engine access log's POST /v1/chat/completions lines over a
# run's span, divided by that run's browser turns.
BROWSER_ENGINE_CALLS_PER_TURN: Final[int] = 3
# A released-prefix leak on the raw route and a flash from a seat are failures;
# this is the one policy both fields share, enforced by the Filter stream hook
# (ticket 10, v0.1.18).
STREAM_LEAK_FAILS: Final[bool] = True
# The browser mode drains its frame observer this often; the observer captures
# every painted frame regardless, so this is the drain interval, not the
# resolution of what the screen showed — a starting value.
LIVE_POLL_SECONDS: Final[float] = 1.0


def _cleanup_fix(account: str) -> str:
    return f"Delete the listed chats as {account} through the frontend, then retry."


def _unverified_fix(account: str) -> str:
    return f"List {account}'s chats and delete any carrying this run's sentinel, then retry."


_RECORD_FIX: Final[str] = "Check the --out directory's disk and permissions, then retry."



@dataclass(frozen=True, slots=True)
class RunSpec:
    """The immutable choices and identity of one turn-harness run."""

    cases: Path
    repeat: int
    stream: bool
    out: Path | None
    force: bool
    dry_run: bool
    sentinel: str
    model: str = GENERAL_PRESET_ID
    base_model: str | None = None
    browser: bool = False
    probe_inlet: bool = False
    trust_ca: bool = False
    concurrent: int = 1
    unfiltered: bool = False
    case_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UnfilteredTurn:
    """The bare completion and the stored-chat watch for one unfiltered turn."""

    status: int
    body: object | None
    watch: session.ChatWatch


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """The data common to every mode's completed turn."""

    chat_id: str | None
    assistant: Mapping[str, object] | None
    user: Mapping[str, object] | None
    elapsed: float | None
    problem: Problem | None
    candidates: int | None = None
    unidentified: Problem | None = None
    extras: object | None = None
    user_id: str = ""
    assistant_id: str = ""
    started: float | None = None


@dataclass(frozen=True, slots=True)
class TurnRow:
    """One completed case turn and the bookkeeping facts it contributes."""

    result: StageResult
    record: dict[str, object] | None
    kind: str
    verdict_kind: str | None
    missed: bool
    chat_id: str | None
    deleted: bool
    unverified: bool
    stream_kind: str | None
    session: int
    started: float | None
    offline_kind: str | None = None
    stored_deleted: tuple[str, ...] = ()
    stored_not_deleted: tuple[str, ...] = ()


class TurnDriver(Protocol):
    """The mode-specific sign-in and turn behind the one per-case loop.

    ``signin`` returns the ``signin`` row's detail and leaves the driver holding
    its client; ``turn`` makes one turn and returns what the loop judges. The
    loop keeps each turn's before-listing, the classification, the
    judgement, the record, and the deletion — nothing of that is a driver's.
    """

    @property
    def client(self) -> Client: ...

    @property
    def account(self) -> str: ...

    def signin(self) -> str: ...

    def observe(self) -> browser.Observation | Problem | None: ...

    @property
    def observation(self) -> browser.Observation | None: ...

    def turn(
        self,
        case: Case,
        prompt: str,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
        ids_before: frozenset[str],
        row_name: str,
    ) -> TurnOutcome: ...


class ApiTurnDriver:
    """Drive the existing managed-turn API path behind the turn seam."""

    def __init__(
        self,
        client_factory: Callable[..., Client],
        password: str,
        model: str = GENERAL_PRESET_ID,
    ) -> None:
        self._client_factory = client_factory
        self._password = password
        self._model = model
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("the API driver has not signed in")
        return self._client

    @property
    def account(self) -> str:
        return EVAL_IDENTITY.username

    def observe(self) -> None:
        """The API mode has no screen to observe."""

    @property
    def observation(self) -> browser.Observation | None:
        return None

    def signin(self) -> str:
        """Sign in as the eval identity and return the ``signin`` row's detail."""

        token = owuiturn.signin(
            self._client_factory,
            EVAL_IDENTITY.email,
            self._password,
        )
        self._client = self._client_factory(token=token)
        leftover = len(owuiturn.chat_ids(self._client))
        return f"signed in as gideon-eval; leftover chats: {leftover}"

    def turn(
        self,
        case: Case,
        prompt: str,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
        ids_before: frozenset[str],
        row_name: str,
    ) -> TurnOutcome:
        del row_name
        user_id = str(uuid.uuid4())
        assistant_id = str(uuid.uuid4())
        started = monotonic()
        # An unsearched case's body carries no features key (ticket 16).
        managed_problem = owuiturn.managed_turn(
            self.client,
            model=self._model,
            prompt=prompt,
            user_id=user_id,
            assistant_id=assistant_id,
            timestamp=_timestamp(now),
            features={"web_search": True} if case.search else None,
        )
        elapsed = monotonic() - started

        def find() -> owuiturn.TurnChat:
            return owuiturn.find_turn_chat(
                self.client,
                ids_before=ids_before,
                user_id=user_id,
                assistant_id=assistant_id,
            )

        # The turn is made whatever the finder answers, so a refused or failed
        # turn's chat is identified and deleted too. One failed listing after
        # the turn never leaves a chat behind: the finder runs once more, and
        # the first failure is what the row reports. A second failure is the
        # loop's to catch.
        finder_problem: Problem | None = None
        try:
            found = find()
        except owui.OwuiError as exc:
            finder_problem = Problem(exc.problem, exc.fix)
            found = find()
        stored = found.stored
        stored_problem = stored.problem if stored is not None else None
        return TurnOutcome(
            chat_id=found.chat_id,
            assistant=stored.assistant if stored is not None else None,
            user=stored.user if stored is not None else None,
            elapsed=elapsed,
            problem=managed_problem or finder_problem or stored_problem,
            candidates=found.candidates,
            unidentified=found.problem,
            user_id=user_id,
            assistant_id=assistant_id,
            started=started,
        )


class UnfilteredTurnDriver(ApiTurnDriver):
    """Drive one bare non-streaming completion: the eval identity's sign-in, no stored chat."""

    def __init__(
        self,
        client_factory: Callable[..., Client],
        password: str,
        *,
        model: str = GENERAL_PRESET_ID,
        sentinel: str,
    ) -> None:
        super().__init__(client_factory, password, model)
        self._sentinel = sentinel

    def turn(
        self,
        case: Case,
        prompt: str,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
        ids_before: frozenset[str],
        row_name: str,
    ) -> TurnOutcome:
        del case, now, row_name
        started = monotonic()
        response = session.post_unfiltered(self.client, self._model, prompt)
        elapsed = monotonic() - started
        watch = session.watch_chats(
            self.client,
            ids_before=ids_before,
            sentinel=self._sentinel,
        )
        problem = response.problem
        if problem is None and response.status != 200:
            problem = Problem(
                f"Open WebUI returned HTTP {response.status}.", session.LOGS_FIX
            )
        if problem is None and classify.probe_answer(response.body) is None:
            problem = Problem("completion has no string content.", session.LOGS_FIX)
        return TurnOutcome(
            chat_id=None,
            assistant=None,
            user=None,
            elapsed=elapsed,
            problem=problem,
            candidates=watch.candidates,
            extras=UnfilteredTurn(response.status, response.body, watch),
            started=started,
        )


@dataclass(frozen=True, slots=True)
class BrowserSetup:
    """What the browser mode launched and reads: the page, its teardown, and the seams."""

    page: browser.Page
    close: Callable[[], None]
    detail: str
    hostname: str
    account: str
    request_log: chromium.RequestLog
    page_timeout: float
    poll: float = LIVE_POLL_SECONDS


class BrowserTurnDriver:
    """Drive one browser page, then read its chat through the API client with the page's token."""

    def __init__(
        self,
        setup: BrowserSetup,
        client_factory: Callable[..., Client],
        password: str,
        *,
        guardrail: Any,
        out: Path,
    ) -> None:
        self._setup = setup
        self._client_factory = client_factory
        self._password = password
        self._guardrail = guardrail
        self._out = out
        self._client: Client | None = None
        self._observation: browser.Observation | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("the browser driver has not signed in")
        return self._client

    @property
    def account(self) -> str:
        return self._setup.account

    def observe(self) -> browser.Observation | Problem:
        """The page-only observations, once, before the first case; kept for the run record."""

        result = browser.observe(
            self._setup.page, self._out, timeout=self._setup.page_timeout
        )
        if not isinstance(result, Problem):
            self._observation = result
        return result

    @property
    def observation(self) -> browser.Observation | None:
        return self._observation

    def signin(self) -> str:
        """Sign in through the page and retain only the authenticated client."""

        result = browser.signin(
            self._setup.page,
            self._setup.account,
            self._password,
            timeout=self._setup.page_timeout,
        )
        if isinstance(result, Problem):
            raise owui.OwuiError(result.problem, result.fix)
        self._client = self._client_factory(token=result)
        leftover = len(owuiturn.chat_ids(self.client))
        return (
            f"signed in as {self._setup.account} through the LDAP form; "
            f"leftover chats: {leftover}"
        )

    def turn(
        self,
        case: Case,
        prompt: str,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
        ids_before: frozenset[str],
        row_name: str,
    ) -> TurnOutcome:
        del now, ids_before
        browser_turn = browser.turn(
            self._setup.page,
            prompt,
            judge=classify.live_judge(self._guardrail, case.prompt),
            is_replacement=classify.refusal_test(self._guardrail),
            monotonic=monotonic,
            poll=self._setup.poll,
            page_timeout=self._setup.page_timeout,
            deadline=monotonic() + TURN_TIMEOUT_SECONDS,
            out=self._out,
            row_name=row_name,
        )
        stored = owuiturn.StoredTurn()
        stored_problem = browser_turn.problem
        if browser_turn.chat_id is not None:
            try:
                stored = owuiturn.stored_current(self.client, browser_turn.chat_id)
            except owui.OwuiError as exc:
                stored = owuiturn.StoredTurn(problem=Problem(exc.problem, exc.fix))
            # The page's problem first, then the record's: an errored or
            # unfinished stored message is never classified.
            stored_problem = stored_problem or stored.problem
        return TurnOutcome(
            chat_id=browser_turn.chat_id,
            assistant=stored.assistant,
            user=stored.user,
            elapsed=browser_turn.elapsed,
            problem=stored_problem,
            extras=browser_turn,
        )


def _record_home(spec: RunSpec) -> str:
    """Where a row's record is, or how to keep one — never a file that was not written."""

    if spec.out is not None:
        return f"the record in {spec.out}"
    return "the record (re-run with --out <dir> to keep one)"


def _turn_fix(spec: RunSpec) -> str:
    return f"Read {_record_home(spec)}, then retry."


def new_sentinel() -> str:
    """Eight hex characters naming one run in every prompt, never case text."""

    return secrets.token_hex(4)


def _timestamp(now: Callable[[], datetime]) -> int:
    """The user message's timestamp in the browser's form: whole epoch seconds."""

    value = now()
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


def _internal_error(row_name: str, exc: Exception, fix: str) -> StageResult:
    return StageResult(
        row_name,
        False,
        f"internal error: {type(exc).__name__}: {exc}",
        fix,
    )


def _problem_data(problem: Problem | None) -> dict[str, str] | None:
    if problem is None:
        return None
    return {"problem": problem.problem, "fix": problem.fix}


def _case_data(case: Case) -> dict[str, object]:
    return {
        "id": case.id,
        "prompt": case.prompt,
        "expect": case.expect,
        "must": [pattern.pattern for pattern in case.must],
        "must_not": [pattern.pattern for pattern in case.must_not],
        "block": case.block,
        "kind": case.kind,
        "search": case.search,
        "sources": case.sources,
    }


def _verdict_data(verdict: classify.Verdict | None) -> dict[str, object] | None:
    if verdict is None:
        return None
    return {
        "kind": verdict.kind,
        "pattern_id": verdict.pattern_id,
        "tripped_in": verdict.tripped_in,
        "block_present": verdict.block_present,
        "reasoning_stored": verdict.reasoning_stored,
        "sources_present": verdict.sources_present,
        "length": verdict.length,
    }


def _stream_data(
    capture: session.StreamCapture | None,
    verdict: classify.StreamVerdict | None,
) -> dict[str, object] | None:
    if capture is None:
        return None
    verdict_data = None
    if verdict is not None:
        verdict_data = {
            "clean": verdict.clean,
            "pattern_id": verdict.pattern_id,
            "offset": verdict.offset,
        }
    return {
        "deltas": [list(delta) for delta in capture.deltas],
        "elapsed": capture.elapsed,
        "verdict": verdict_data,
        "problem": _problem_data(capture.problem),
    }


def _browser_turn_fix(spec: RunSpec, turn: browser.BrowserTurn) -> str:
    return (
        f"Read {_record_home(spec)} and screenshot {turn.screenshot}, then retry the browser turn."
    )


def _browser_data(
    turn: browser.BrowserTurn, verdict: classify.LiveVerdict
) -> dict[str, object]:
    """The record's browser block: every state compact, the notable states' texts."""

    return {
        "chat_id": turn.chat_id,
        "screenshot": str(turn.screenshot),
        "block_opened_at": turn.block_opened_at,
        "elapsed": turn.elapsed,
        "regions": dict(turn.regions),
        "no_states": verdict.no_states,
        "no_frames": verdict.no_frames,
        "first_trip_index": verdict.first_trip_index,
        "replaced_index": verdict.replaced_index,
        "gone_index": verdict.gone_index,
        "first_trip_at": verdict.first_trip_at,
        "replaced_at": verdict.replaced_at,
        "gone_at": verdict.gone_at,
        "refused_index": verdict.refused_index,
        "refused_at": verdict.refused_at,
        "ended_at": verdict.ended_at,
        "on_screen_at_end": verdict.on_screen_at_end,
        "reasoning_painted_at": verdict.reasoning_painted_at,
        "states": [classify.entry_record(entry) for entry in turn.entries],
        "texts": {
            str(index): {"block": block, "answer": answer}
            for index, (block, answer) in sorted(turn.texts.items())
        },
    }


def _observation_detail(observation: browser.Observation) -> str:
    models = ", ".join(observation.models)
    plus_items = ", ".join(observation.plus_items)
    if observation.integrations:
        integrations = ", ".join(
            f"{name} {'on' if pressed else 'off'}"
            for name, pressed in observation.integrations
        )
    else:
        integrations = "none"
    return (
        f"selector: {observation.selector_label} ({len(observation.models)} listed: {models}); "
        f"+ menu: {plus_items}; integrations: {integrations}"
    )


def _case_record(
    case: Case,
    spec: RunSpec,
    *,
    session: int,
    chat_id: str | None,
    candidates: int | None,
    user_id: str,
    assistant_id: str,
    assistant: Mapping[str, object] | None,
    user: Mapping[str, object] | None,
    verdict: classify.Verdict | None,
    checks: Mapping[str, bool],
    elapsed: float | None,
    problem: Problem | None,
    stream_capture: session.StreamCapture | None,
    stream_verdict: classify.StreamVerdict | None,
    browser_turn: browser.BrowserTurn | None,
    live_verdict: classify.LiveVerdict | None,
) -> dict[str, object]:
    browser_data = None
    if browser_turn is not None and live_verdict is not None:
        browser_data = _browser_data(browser_turn, live_verdict)
    return {
        "case": _case_data(case),
        "sentinel": spec.sentinel,
        "session": session,
        # Filled by the bookkeeping, relative to its origin.
        "started": None,
        "chat_id": chat_id,
        "candidates": candidates,
        "user_id": user_id,
        "assistant_id": assistant_id,
        "assistant": assistant,
        "user": user,
        "verdict": _verdict_data(verdict),
        "checks": dict(checks),
        "elapsed": elapsed,
        "problem": _problem_data(problem),
        "stream": _stream_data(stream_capture, stream_verdict),
        "browser": browser_data,
    }


def _offline_data(
    judgement: classify.OfflineJudgement | None,
) -> dict[str, object] | None:
    if judgement is None:
        return None
    return {
        "family": judgement.family,
        "pattern_id": judgement.pattern_id,
        "supplied": {
            family: sorted(figures)
            for family, figures in judgement.supplied.items()
        },
        "unsupplied": {
            family: sorted(figures)
            for family, figures in judgement.unsupplied.items()
        },
        "hits": [
            {
                "family": hit.family,
                "pattern_id": hit.pattern_id,
                "start": hit.start,
                "end": hit.end,
                "text": hit.text,
            }
            for hit in judgement.hits
        ],
    }


def _unfiltered_record(
    case: Case,
    spec: RunSpec,
    *,
    session_number: int,
    prompt: str,
    turn: UnfilteredTurn | None,
    judgement: classify.OfflineJudgement | None,
    problem: Problem | None,
    elapsed: float | None,
    deletions: Mapping[str, Problem | None],
) -> dict[str, object]:
    watch = turn.watch if turn is not None else session.ChatWatch((), 0)
    return {
        "case": _case_data(case),
        "sentinel": spec.sentinel,
        "session": session_number,
        "started": None,
        "prompt": prompt,
        "status": turn.status if turn is not None else None,
        "body": turn.body if turn is not None else None,
        "elapsed": elapsed,
        "judgement": _offline_data(judgement),
        "watch": {
            "candidates": watch.candidates,
            "stored_ids": list(watch.stored_ids),
            "problem": _problem_data(watch.problem),
        },
        "deletions": {
            chat_id: {
                "deleted": deletion is None,
                "problem": _problem_data(deletion),
            }
            for chat_id, deletion in deletions.items()
        },
        "problem": _problem_data(problem),
    }


def _write_json(host: Host, path: Path, value: Mapping[str, object]) -> Problem | None:
    """Write one record; a failed write is a problem for a row, never a traceback."""

    try:
        host.write_text(
            path,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    except OSError as exc:
        return Problem(f"record not written: {path} ({exc})", _RECORD_FIX)
    return None


def _write_requests_log(
    host: Host, setup: BrowserSetup | None, output: Path | None
) -> Problem | None:
    """Write the request evidence whole; after every browser row, on the requests row, and at teardown.

    The per-row writes are best effort so a killed run still leaves the last
    row's requests on disk; the ``requests`` row is where a failed write shows.
    """

    if setup is None or output is None:
        return None
    try:
        host.write_text(output / "requests.log", setup.request_log.render() + "\n")
    except OSError as exc:
        return Problem(f"requests.log not written ({exc})", _RECORD_FIX)
    return None


def _emit_stage(
    result: StageResult,
    *,
    host: Host,
    output: Path | None,
    browser_setup: BrowserSetup | None,
) -> None:
    print_stage(result)
    _write_requests_log(host, browser_setup, output)


def _probe_data(
    response: session.ProbeResponse, verdict: str | None
) -> dict[str, object]:
    return {
        "status": response.status,
        "body": response.body,
        "verdict": verdict,
        "problem": _problem_data(response.problem),
    }


def _probe_result(
    name: str, response: session.ProbeResponse, ok: bool, detail: str, fix: str
) -> StageResult:
    """The probe row: a request that failed carries its own problem and fix, never an HTTP 0."""

    if response.problem is not None:
        return StageResult(name, False, f"probe error: {response.problem.problem}", response.problem.fix)
    return StageResult(name, ok, detail, fix)


def _browser_run_data(
    setup: BrowserSetup,
    observation: browser.Observation | None,
    probe_records: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    counts = setup.request_log.counts()
    observation_data: dict[str, object] | None = None
    if observation is not None:
        observation_data = {
            "selector_label": observation.selector_label,
            "models": list(observation.models),
            "plus_items": list(observation.plus_items),
            "integrations": [list(value) for value in observation.integrations],
        }
    data: dict[str, object] = {
        "detail": setup.detail,
        "hostname": setup.hostname,
        "footprint": {
            "harness_home": str(chromium.HARNESS_HOME),
            "browsers_dir": str(chromium.BROWSERS_DIR),
            "browser_home": str(chromium.BROWSER_HOME),
            "trust_store": str(chromium.TRUST_STORE_PATH),
        },
        "requests": {
            "on_host": counts.on_host,
            "off_host": counts.off_host,
            "off_hostnames": list(counts.off_hostnames),
        },
        "observation": observation_data,
    }
    if probe_records:
        data["probes"] = {
            name: {
                key: value[key]
                for key in ("ok", "status", "residual")
                if key in value
            }
            for name, value in probe_records.items()
        }
    return data


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """What the inlet probe's three rows produced: their records and their accounting.

    ``turns`` is the engine calls attempted — one when the chat-id completion
    was posted, one more when the base-model completion answered (the gate
    failed open and the request reached the engine), none when the probe
    never got that far.
    """

    records: dict[str, dict[str, object]]
    misses: int
    deleted: int
    not_deleted: tuple[str, ...]
    turns: int


def _probe_row(
    host: Host,
    spec: RunSpec,
    result: StageResult,
    response: session.ProbeResponse,
    pattern: str | None,
    records: dict[str, dict[str, object]],
    *,
    residual: str | None,
) -> StageResult:
    """Record one probe row and write its ``--out`` file; a failed write fails the row."""

    records[result.name] = {"ok": result.ok, **_probe_data(response, pattern), "residual": residual}
    if spec.out is not None:
        write_problem = _write_json(
            host, spec.out / f"{result.name}.json", _probe_data(response, pattern)
        )
        if write_problem is not None:
            records[result.name]["ok"] = False
            return StageResult(result.name, False, write_problem.problem, write_problem.fix)
    return result


def _run_probe(
    client: Client,
    spec: RunSpec,
    guardrail: Any,
    host: Host,
    emit: Callable[[StageResult], None],
) -> ProbeOutcome:
    """Ticket 34's inlet-gate probe from the users seat, three rows.

    ``inlet-bare`` posts a completion carrying neither a session id nor a chat
    id and expects the Filter's session refusal on a 400 — refused before the
    engine, a row and never a turn. ``inlet-chat-id`` adds the id of a chat
    the account owns and expects the model's answer unreplaced: §16's recorded
    residual, judged and reported, never failed on its content.
    ``inlet-base-model`` reuses that owned chat with the base model and expects
    the branch refusal before the engine. Neither completion should create a
    chat of its own; any that appears is deleted, and the owned chat is deleted
    whatever happened. A request that fails is a failed row for whichever
    probe had not been reported, never a traceback.
    """

    prompt = session.prompt_text("inlet-probe", spec.sentinel, session.PROBE_PROMPT)
    records: dict[str, dict[str, object]] = {}
    misses = 0
    deleted = 0
    not_deleted: list[str] = []
    emitted: set[str] = set()
    probe_chat: str | None = None
    probe_chat_deleted = False
    turns = 0

    def report(result: StageResult) -> None:
        nonlocal misses
        emit(result)
        emitted.add(result.name)
        misses += int(not result.ok)

    try:
        before = owuiturn.chat_ids(client)
        bare = session.probe_bare(client, spec.model, prompt)
        bare_ok = (
            bare.problem is None
            and bare.status == 400
            and isinstance(bare.body, Mapping)
            and bare.body.get("detail") == guardrail.SESSION_REFUSAL
        )
        report(
            _probe_row(
                host,
                spec,
                _probe_result(
                    "inlet-bare",
                    bare,
                    bare_ok,
                    "refused with the session refusal (HTTP 400)"
                    if bare_ok
                    else f"HTTP {bare.status}, not the session refusal",
                    "" if bare_ok else _turn_fix(spec),
                ),
                bare,
                None,
                records,
                residual=None,
            )
        )

        # The chat id must be a chat the account owns (the frontend answers
        # 404 otherwise), so an empty one is made through the frontend's own
        # route — no engine call — and kept until both completion probes finish.
        probe_chat = session.new_chat(client, spec.model)
        turns = 1
        with_chat = session.probe_with_chat_id(client, spec.model, prompt, probe_chat)
        answer = classify.probe_answer(with_chat.body)
        pattern = classify.probe_verdict(guardrail, with_chat.body, prompt)
        chat_ok = (
            with_chat.problem is None
            and with_chat.status == 200
            and answer is not None
            and bool(answer.strip())
        )
        residual: str | None = None
        if chat_ok:
            residual = (
                f"unreplaced, computed@{pattern}" if pattern is not None else "unreplaced, clean"
            )
        chat_result = _probe_row(
            host,
            spec,
            _probe_result(
                "inlet-chat-id",
                with_chat,
                chat_ok,
                f"residual: {residual}" if chat_ok else f"HTTP {with_chat.status}, no answer",
                "" if chat_ok else _turn_fix(spec),
            ),
            with_chat,
            pattern,
            records,
            residual=residual,
        )
        # The base-model completion is posted before the stray sweep, so a chat
        # either completion might create is swept; its row follows the chat-id row.
        if spec.base_model is None:
            raise RuntimeError("the probe base model is unavailable")
        base = session.probe_with_chat_id(client, spec.base_model, prompt, probe_chat)
        if base.problem is None and base.status == 200:
            # The gate failed open: the completion reached the engine, so it counts.
            turns += 1
        strays = 0
        for stray in sorted(owuiturn.chat_ids(client) - before - {probe_chat}):
            if owuiturn.delete_chat(client, stray) is None:
                deleted += 1
                strays += 1
            else:
                not_deleted.append(stray)
        if strays:
            chat_result = StageResult(
                chat_result.name,
                chat_result.ok,
                f"{chat_result.detail}; stray chat deleted",
                chat_result.fix,
            )
        report(chat_result)

        base_ok = (
            base.problem is None
            and base.status == 400
            and isinstance(base.body, Mapping)
            and base.body.get("detail") == guardrail.BRANCH_REFUSAL
        )
        report(
            _probe_row(
                host,
                spec,
                _probe_result(
                    "inlet-base-model",
                    base,
                    base_ok,
                    "refused with the branch refusal (HTTP 400)"
                    if base_ok
                    else f"HTTP {base.status}, not the branch refusal",
                    "" if base_ok else _turn_fix(spec),
                ),
                base,
                None,
                records,
                residual=None,
            )
        )

        if owuiturn.delete_chat(client, probe_chat) is None:
            deleted += 1
        else:
            not_deleted.append(probe_chat)
        probe_chat_deleted = True
    except owui.OwuiError as exc:
        for name in ("inlet-bare", "inlet-chat-id", "inlet-base-model"):
            if name not in emitted:
                records[name] = {"ok": False, "status": 0, "body": None, "verdict": None, "residual": None, "problem": {"problem": exc.problem, "fix": exc.fix}}
                report(StageResult(name, False, f"probe error: {exc.problem}", exc.fix))
    except Exception as exc:  # noqa: BLE001 - the probe boundary owns the traceback.
        for name in ("inlet-bare", "inlet-chat-id", "inlet-base-model"):
            if name not in emitted:
                records[name] = {"ok": False, "status": 0, "body": None, "verdict": None, "residual": None, "problem": None}
                report(_internal_error(name, exc, _turn_fix(spec)))
    finally:
        if probe_chat is not None and not probe_chat_deleted:
            try:
                deletion = owuiturn.delete_chat(client, probe_chat)
            except Exception:  # noqa: BLE001 - cleanup must reach its row.
                deletion = Problem("chat deletion raised an internal error", _turn_fix(spec))
            if deletion is None:
                deleted += 1
            else:
                not_deleted.append(probe_chat)
    return ProbeOutcome(records, misses, deleted, tuple(not_deleted), turns)


def _summary_counts(
    totals: Mapping[str, int],
    counts: Mapping[str, Mapping[str, int]],
    misses: int,
    stream_counts: Mapping[str, int] | None = None,
    extra_turns: int = 0,
    offline_counts: Mapping[str, int] | None = None,
) -> dict[str, object]:
    if offline_counts is not None:
        return {
            "turns": sum(offline_counts.values()),
            "tripped": {
                kind: count
                for kind, count in offline_counts.items()
                if kind not in {"clean", "error"} and count
            },
            "clean": offline_counts.get("clean", 0),
            "errors": offline_counts.get("error", 0),
        }
    # A turn is a row: every case row, plus every replay and the probe's call;
    # the guard's engine-call count lives in cli.py.
    replays = sum(stream_counts.values()) if stream_counts is not None else 0
    summary: dict[str, object] = {
        "turns": sum(totals.values()) + replays + extra_turns,
        "misses": misses,
    }
    for kind in CASE_KINDS:
        if kind in totals:
            summary[kind] = {
                "total": totals[kind],
                **{class_name: counts[kind][class_name] for class_name in classify.KINDS},
            }
    if stream_counts is not None:
        summary["stream"] = dict(stream_counts)
    return summary


def _summary_detail(
    totals: Mapping[str, int],
    counts: Mapping[str, Mapping[str, int]],
    misses: int,
    stream_counts: Mapping[str, int] | None = None,
    extra_turns: int = 0,
    offline_counts: Mapping[str, int] | None = None,
) -> str:
    if offline_counts is not None:
        tripped = ", ".join(
            f"{kind} {count}"
            for kind, count in offline_counts.items()
            if kind not in {"clean", "error"} and count
        )
        return (
            f"{sum(offline_counts.values())} turns; "
            f"tripped {tripped or 'none'}; "
            f"clean {offline_counts.get('clean', 0)}; "
            f"errors {offline_counts.get('error', 0)}"
        )
    replays = sum(stream_counts.values()) if stream_counts is not None else 0
    parts = [f"{sum(totals.values()) + replays + extra_turns} turns"]
    labels = {"positive": "positives", "control": "controls", "case": "cases"}
    for kind in CASE_KINDS:
        if kind in totals:
            class_counts = ", ".join(
                f"{class_name} {counts[kind][class_name]}"
                for class_name in classify.KINDS
            )
            parts.append(f"{labels[kind]} {totals[kind]}: {class_counts}")
    parts.append(f"misses {misses}")
    if stream_counts is not None:
        parts.append(
            "stream: "
            f"{stream_counts['clean']} clean, "
            f"{stream_counts['leak']} leak, "
            f"{stream_counts['error']} error"
        )
    return "; ".join(parts)


def _replay(
    client: Client,
    spec: RunSpec,
    guardrail: Any,
    prompt: str,
    monotonic: Callable[[], float],
) -> tuple[session.StreamCapture, classify.StreamVerdict | None, str, bool, str]:
    """The raw-route replay after a case's managed turn.

    Returns the capture, its verdict, the row's suffix, whether the row fails,
    and the stream verdict kind: a stream the tool could not observe always
    fails it; a released prefix leak fails it under the shared stream policy.
    """

    capture = session.raw_stream(
        client,
        model=spec.model,
        prompt=prompt,
        deadline=monotonic() + TURN_TIMEOUT_SECONDS,
        monotonic=monotonic,
    )
    if capture.problem is not None:
        return capture, None, f"; stream error: {capture.problem.problem}", True, "error"
    verdict = classify.stream_verdict(guardrail, capture.deltas, prompt)
    if verdict.clean:
        return capture, verdict, "; stream clean", False, "clean"
    suffix = f"; stream leak@{verdict.pattern_id} at {verdict.offset} chars"
    return capture, verdict, suffix, STREAM_LEAK_FAILS, "leak"


def _units(cases: Sequence[Case], repeat: int) -> tuple[tuple[Case, int], ...]:
    """Return the cases and repetitions in the harness's sequential order."""

    return tuple(
        (case, repetition)
        for case in cases
        for repetition in range(1, repeat + 1)
    )


def _session_units(
    units: tuple[tuple[Case, int], ...], session: int
) -> tuple[tuple[Case, int], ...]:
    """Rotate the sequential units so concurrent sessions drive different cases."""

    if not units:
        return ()
    offset = (session - 1) % len(units)
    return units[offset:] + units[:offset]


def _row_name(case: Case, repetition: int, session: int, spec: RunSpec) -> str:
    """Name one case row according to repetition and session."""

    name = f"{case.id}#{repetition}" if spec.repeat > 1 else case.id
    return f"{name}@{session}" if spec.concurrent > 1 else name


@dataclass(slots=True)
class _Bookkeeping:
    """The case rows' totals and cleanup facts shared by concurrent sessions."""

    totals: dict[str, int]
    counts: dict[str, dict[str, int]]
    offline_counts: dict[str, int] = field(default_factory=dict)
    deleted: int = 0
    stored: int = 0
    not_deleted: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    misses: int = 0
    all_ok: bool = True
    stream_counts: dict[str, int] = field(
        default_factory=lambda: {"clean": 0, "leak": 0, "error": 0}
    )
    # The monotonic instant ``started`` is measured from: read before the
    # sessions start under --concurrent, else the first turn's own send, so a
    # single session reads the clock no more often than before.
    origin: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def absorb(self, row: TurnRow) -> None:
        """Fold one finalized row into the run's totals."""

        self.totals[row.kind] += 1
        if row.verdict_kind is not None:
            self.counts[row.kind][row.verdict_kind] += 1
        if row.stream_kind is not None:
            self.stream_counts[row.stream_kind] += 1
        if row.offline_kind is not None:
            self.offline_counts[row.offline_kind] = (
                self.offline_counts.get(row.offline_kind, 0) + 1
            )
        self.stored += len(row.stored_deleted) + len(row.stored_not_deleted)
        self.deleted += len(row.stored_deleted)
        self.not_deleted.extend(row.stored_not_deleted)
        if row.deleted:
            self.deleted += 1
        elif row.chat_id is not None:
            self.not_deleted.append(row.chat_id)
        if row.unverified:
            self.unverified.append(row.result.name)
        if row.missed:
            self.misses += 1
        self.all_ok = self.all_ok and row.result.ok

    def finalise(
        self,
        row: TurnRow,
        *,
        spec: RunSpec,
        host: Host,
        emit: Callable[[StageResult], None],
    ) -> None:
        """Write, absorb, and emit one row atomically in that order."""

        with self.lock:
            finalized = row
            if row.started is not None and self.origin is None:
                self.origin = row.started
            if row.record is not None:
                row.record["started"] = (
                    None
                    if row.started is None or self.origin is None
                    else row.started - self.origin
                )
            if spec.out is not None and row.record is not None:
                write_problem = _write_json(
                    host, spec.out / f"{row.result.name}.json", row.record
                )
                if write_problem is not None:
                    finalized = replace(
                        row,
                        result=StageResult(
                            row.result.name,
                            False,
                            f"{row.result.detail}; {write_problem.problem}",
                            write_problem.fix,
                        ),
                        missed=True,
                        offline_kind=(
                            "error" if row.offline_kind is not None else None
                        ),
                    )
            self.absorb(finalized)
            emit(finalized.result)


def _run_turn(
    spec: RunSpec,
    *,
    client: Client,
    driver: TurnDriver,
    guardrail: Any,
    case: Case,
    session_number: int,
    row_name: str,
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
) -> TurnRow:
    """Run one case, including its judgment and cleanup, and return its facts.

    The record's ``started`` is the bookkeeping's to fill, under its lock.
    """

    chat_id: str | None = None
    candidates: int | None = None
    user_id = ""
    assistant_id = ""
    assistant: Mapping[str, object] | None = None
    user: Mapping[str, object] | None = None
    verdict: classify.Verdict | None = None
    verdict_kind: str | None = None
    checks: dict[str, bool] = {}
    problem: Problem | None = None
    unidentified: Problem | None = None
    elapsed: float | None = None
    stream_capture: session.StreamCapture | None = None
    stream_verdict: classify.StreamVerdict | None = None
    stream_kind: str | None = None
    browser_turn: browser.BrowserTurn | None = None
    live_verdict: classify.LiveVerdict | None = None
    deleted = False
    unverified = False
    started: float | None = None
    result: StageResult
    try:
        ids_before = owuiturn.chat_ids(client)
        prompt = session.prompt_text(
            case.id, spec.sentinel, case.prompt
        )
        outcome = driver.turn(
            case,
            prompt,
            now=now,
            monotonic=monotonic,
            ids_before=ids_before,
            row_name=row_name,
        )
        user_id = outcome.user_id
        assistant_id = outcome.assistant_id
        chat_id = outcome.chat_id
        candidates = outcome.candidates
        assistant = outcome.assistant
        user = outcome.user
        elapsed = outcome.elapsed
        started = outcome.started
        problem = outcome.problem
        unidentified = outcome.unidentified
        if isinstance(outcome.extras, browser.BrowserTurn):
            browser_turn = outcome.extras
            live_verdict = classify.live_verdict(browser_turn.entries)
        live_detail = (
            f"; {classify.live_field(live_verdict)}"
            if live_verdict is not None
            else ""
        )
        if problem is not None:
            result = StageResult(
                row_name,
                False,
                f"turn error: {problem.problem}{live_detail}",
                _browser_turn_fix(spec, browser_turn)
                if browser_turn is not None
                else _turn_fix(spec),
            )
        elif unidentified is not None:
            result = StageResult(
                row_name,
                False,
                f"{unidentified.problem}{live_detail}",
                _unverified_fix(driver.account),
            )
        elif assistant is None:
            problem = Problem("assistant message is missing", _turn_fix(spec))
            result = StageResult(
                row_name,
                False,
                f"turn error: assistant message is missing{live_detail}",
                _turn_fix(spec),
            )
        else:
            verdict = classify.classify(guardrail, assistant, user)
            content = assistant.get("content")
            text = content if isinstance(content, str) else ""
            judgement = classify.judge_case(
                case, verdict, text, record=_record_home(spec)
            )
            checks = dict(judgement.checks)
            verdict_kind = verdict.kind
            row_ok, row_fix, stream_detail = judgement.ok, judgement.fix, ""
            if (
                live_verdict is not None
                and browser_turn is not None
                and classify.live_fails(live_verdict, STREAM_LEAK_FAILS)
            ):
                row_ok = False
                row_fix = _browser_turn_fix(spec, browser_turn)
            if spec.stream:
                (
                    stream_capture,
                    stream_verdict,
                    stream_detail,
                    stream_failed,
                    stream_kind,
                ) = _replay(client, spec, guardrail, prompt, monotonic)
                if stream_failed:
                    row_ok, row_fix = False, _turn_fix(spec)
            result = StageResult(
                row_name,
                row_ok,
                f"{judgement.detail}{live_detail}{stream_detail}; {elapsed:.2f}s",
                row_fix,
            )
    except owui.OwuiError as exc:
        problem = Problem(exc.problem, exc.fix)
        result = StageResult(
            row_name,
            False,
            f"turn error: {exc.problem}",
            _turn_fix(spec),
        )
    except Exception as exc:  # noqa: BLE001 - the case boundary owns the traceback.
        result = _internal_error(row_name, exc, _turn_fix(spec))
        problem = Problem(result.detail, result.fix)
    finally:
        # The finder identifies only this turn's chat; a turn without an
        # identified chat is unverified and leaves no other chat touched.
        if chat_id is None:
            unverified = True
        else:
            try:
                deletion = owuiturn.delete_chat(client, chat_id)
            except Exception:  # noqa: BLE001 - cleanup must reach its row.
                deletion = Problem(
                    "chat deletion raised an internal error",
                    _cleanup_fix(driver.account),
                )
            if deletion is None:
                deleted = True

    record = (
        _case_record(
            case,
            spec,
            session=session_number,
            chat_id=chat_id,
            candidates=candidates,
            user_id=user_id,
            assistant_id=assistant_id,
            assistant=assistant,
            user=user,
            verdict=verdict,
            checks=checks,
            elapsed=elapsed,
            problem=problem,
            stream_capture=stream_capture,
            stream_verdict=stream_verdict,
            browser_turn=browser_turn,
            live_verdict=live_verdict,
        )
        if spec.out is not None
        else None
    )
    return TurnRow(
        result=result,
        record=record,
        kind=case.kind,
        verdict_kind=verdict_kind,
        missed=not result.ok,
        chat_id=chat_id,
        deleted=deleted,
        unverified=unverified,
        stream_kind=stream_kind,
        session=session_number,
        started=started,
    )


def _run_unfiltered_turn(
    spec: RunSpec,
    *,
    client: Client,
    driver: TurnDriver,
    guardrail: Any,
    case: Case,
    session_number: int,
    row_name: str,
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
) -> TurnRow:
    """Run one bare completion, judge it offline, and clean up proven chats."""

    prompt = ""
    turn: UnfilteredTurn | None = None
    watch = session.ChatWatch((), 0)
    judgement: classify.OfflineJudgement | None = None
    problem: Problem | None = None
    elapsed: float | None = None
    started: float | None = None
    deletions: dict[str, Problem | None] = {}
    stored_deleted: list[str] = []
    stored_not_deleted: list[str] = []

    try:
        ids_before = owuiturn.chat_ids(client)
        prompt = session.prompt_text(case.id, spec.sentinel, case.prompt)
        outcome = driver.turn(
            case,
            prompt,
            now=now,
            monotonic=monotonic,
            ids_before=ids_before,
            row_name=row_name,
        )
        elapsed = outcome.elapsed
        started = outcome.started
        problem = outcome.problem
        if isinstance(outcome.extras, UnfilteredTurn):
            turn = outcome.extras
            watch = turn.watch
        elif problem is None:
            problem = Problem("unfiltered turn response is missing.", _turn_fix(spec))
        if problem is None and turn is not None:
            answer = classify.probe_answer(turn.body)
            if answer is None:
                problem = Problem("completion has no string content.", _turn_fix(spec))
            else:
                judgement = classify.offline_judgement(
                    guardrail, answer, prompt
                )
    except owui.OwuiError as exc:
        problem = Problem(exc.problem, exc.fix)
    except Exception as exc:  # noqa: BLE001 - the case boundary owns the traceback.
        problem = Problem(
            f"internal error: {type(exc).__name__}: {exc}", _turn_fix(spec)
        )
    finally:
        for chat_id in watch.stored_ids:
            try:
                deletion = owuiturn.delete_chat(client, chat_id)
            except Exception:  # noqa: BLE001 - cleanup must reach its row.
                deletion = Problem(
                    "chat deletion raised an internal error",
                    _cleanup_fix(driver.account),
                )
            deletions[chat_id] = deletion
            if deletion is None:
                stored_deleted.append(chat_id)
            else:
                stored_not_deleted.append(chat_id)

    offline_kind = (
        judgement.family if judgement is not None and judgement.family is not None else "clean"
    )
    if problem is not None or judgement is None:
        offline_kind = "error"
        result = StageResult(
            row_name,
            False,
            f"turn error: {problem.problem if problem is not None else 'offline judgement is missing'}"
            + (f"; {elapsed:.2f}s" if elapsed is not None else ""),
            _turn_fix(spec),
        )
    else:
        detail = classify.offline_field(judgement)
        if watch.stored_ids:
            detail += (
                f"; {len(watch.stored_ids)} chats stored, "
                f"{len(stored_deleted)} deleted"
            )
        detail += f"; {elapsed:.2f}s" if elapsed is not None else "; elapsed unavailable"
        if stored_not_deleted:
            result = StageResult(
                row_name,
                False,
                detail,
                _cleanup_fix(driver.account),
            )
        elif watch.problem is not None:
            result = StageResult(
                row_name,
                False,
                detail,
                _unverified_fix(driver.account),
            )
        else:
            result = StageResult(row_name, True, detail, "")

    record = (
        _unfiltered_record(
            case,
            spec,
            session_number=session_number,
            prompt=prompt,
            turn=turn,
            judgement=judgement,
            problem=problem,
            elapsed=elapsed,
            deletions=deletions,
        )
        if spec.out is not None
        else None
    )
    return TurnRow(
        result=result,
        record=record,
        kind=case.kind,
        verdict_kind=None,
        missed=not result.ok,
        chat_id=None,
        deleted=False,
        unverified=watch.problem is not None,
        stream_kind=None,
        session=session_number,
        started=started,
        offline_kind=offline_kind,
        stored_deleted=tuple(stored_deleted),
        stored_not_deleted=tuple(stored_not_deleted),
    )


def _arguments(spec: RunSpec) -> dict[str, object]:
    return {
        "cases": str(spec.cases),
        "repeat": spec.repeat,
        "concurrent": spec.concurrent,
        "stream": spec.stream,
        "out": str(spec.out) if spec.out is not None else None,
        "force": spec.force,
        "dry_run": spec.dry_run,
        "browser": spec.browser,
        "probe_inlet": spec.probe_inlet,
        "trust_ca": spec.trust_ca,
        "unfiltered": spec.unfiltered,
        "case_ids": list(spec.case_ids),
    }


def _run_cases(
    spec: RunSpec,
    *,
    cases: Sequence[Case],
    origin: str,
    window: str,
    guardrail: Any,
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
    host: Host,
    drivers: Sequence[TurnDriver],
    browser_setup: BrowserSetup | None,
) -> int:
    def emit(result: StageResult) -> None:
        _emit_stage(
            result,
            host=host,
            output=spec.out,
            browser_setup=browser_setup,
        )

    first_driver = drivers[0]
    signin_details: list[str] = []
    try:
        for driver in drivers:
            signin_details.append(driver.signin())
        client = first_driver.client
    except owui.OwuiError as exc:
        emit(StageResult("signin", False, exc.problem, exc.fix))
        return 1
    # Under --concurrent the first session's detail, prefixed with the count.
    signin_detail = signin_details[0]
    if len(drivers) > 1:
        signin_detail = f"{len(drivers)} sessions {signin_detail}"
    emit(StageResult("signin", True, signin_detail, ""))
    observation = first_driver.observe()
    if isinstance(observation, Problem):
        emit(StageResult("observe", False, observation.problem, observation.fix))
        return 1
    if observation is not None:
        emit(StageResult("observe", True, _observation_detail(observation), ""))

    totals: dict[str, int] = {}
    counts: dict[str, dict[str, int]] = {}
    for case in cases:
        totals.setdefault(case.kind, 0)
        counts.setdefault(case.kind, dict.fromkeys(classify.KINDS, 0))
    offline_counts = (
        dict.fromkeys((*(family.name for family in guardrail.FAMILIES), "clean", "error"), 0)
        if spec.unfiltered
        else {}
    )
    bookkeeping = _Bookkeeping(
        totals,
        counts,
        offline_counts=offline_counts,
        origin=monotonic() if len(drivers) > 1 else None,
    )
    units = _units(cases, spec.repeat)
    run_turn = _run_unfiltered_turn if spec.unfiltered else _run_turn

    def run_session(session_number: int) -> None:
        driver = drivers[session_number - 1]
        for case, repetition in _session_units(units, session_number):
            row = run_turn(
                spec,
                client=driver.client,
                driver=driver,
                guardrail=guardrail,
                case=case,
                session_number=session_number,
                row_name=_row_name(case, repetition, session_number, spec),
                now=now,
                monotonic=monotonic,
            )
            bookkeeping.finalise(row, spec=spec, host=host, emit=emit)

    if len(drivers) == 1:
        run_session(1)
    else:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(drivers))
        try:
            futures = [
                executor.submit(run_session, session_number)
                for session_number in range(1, len(drivers) + 1)
            ]
            for future in futures:
                future.result()
        finally:
            executor.shutdown(wait=True)

    reported_stream = bookkeeping.stream_counts if spec.stream else None
    probe_turns = 0
    probe_records: dict[str, dict[str, object]] = {}
    if spec.probe_inlet:
        probe = _run_probe(client, spec, guardrail, host, emit)
        probe_turns = probe.turns
        probe_records = probe.records
        bookkeeping.misses += probe.misses
        bookkeeping.deleted += probe.deleted
        bookkeeping.not_deleted.extend(probe.not_deleted)
        bookkeeping.all_ok = bookkeeping.all_ok and probe.misses == 0

    reported_offline = bookkeeping.offline_counts if spec.unfiltered else None
    summary_args = (bookkeeping.totals, bookkeeping.counts, bookkeeping.misses, reported_stream)
    summary_counts = _summary_counts(
        *summary_args, extra_turns=probe_turns, offline_counts=reported_offline
    )
    summary_detail = _summary_detail(
        *summary_args, extra_turns=probe_turns, offline_counts=reported_offline
    )
    summary_ok = (
        bookkeeping.offline_counts["error"] == 0
        if spec.unfiltered
        else bookkeeping.misses == 0
    )
    emit(
        StageResult(
            "summary",
            summary_ok,
            summary_detail,
            "" if summary_ok else "Read the failed rows above.",
        )
    )
    cleanup_ok = not bookkeeping.not_deleted and not bookkeeping.unverified
    if cleanup_ok:
        cleanup_detail = (
            f"{bookkeeping.stored} chats stored, {bookkeeping.deleted} deleted"
            if spec.unfiltered
            else f"{bookkeeping.deleted} chats deleted"
        )
        emit(StageResult("cleanup", True, cleanup_detail, ""))
    else:
        details = (
            [f"{bookkeeping.stored} chats stored, {bookkeeping.deleted} deleted"]
            if spec.unfiltered
            else []
        )
        if bookkeeping.not_deleted:
            details.append(
                f"{len(bookkeeping.not_deleted)} chats not deleted: "
                f"{', '.join(sorted(bookkeeping.not_deleted))}"
            )
        if bookkeeping.unverified:
            details.append(
                f"cleanup unverified for {', '.join(bookkeeping.unverified)}"
            )
        emit(
            StageResult(
                "cleanup",
                False,
                "; ".join(details),
                _cleanup_fix(first_driver.account)
                if bookkeeping.not_deleted
                else _unverified_fix(first_driver.account),
            )
        )
    requests_ok = True
    if spec.browser and browser_setup is not None:
        request_counts = browser_setup.request_log.counts()
        if request_counts.off_host:
            off_host_detail = (
                f"{request_counts.off_host} off-host aborted "
                f"({', '.join(request_counts.off_hostnames)})"
            )
        else:
            off_host_detail = "0 off-host"
        # Both request routes enforce the allowlist, so an off-host abort is
        # evidence of the restriction, not a failure; the row fails only when
        # the log that makes it checkable could not be written.
        requests_detail = (
            f"{request_counts.on_host} requests to {browser_setup.hostname}; "
            f"{off_host_detail}"
        )
        log_problem = _write_requests_log(host, browser_setup, spec.out)
        if log_problem is None:
            emit(StageResult("requests", True, requests_detail, ""))
        else:
            requests_ok = False
            emit(
                StageResult(
                    "requests",
                    False,
                    f"{requests_detail}; {log_problem.problem}",
                    log_problem.fix,
                )
            )
    record_problem: Problem | None = None
    if spec.out is not None:
        run_data: dict[str, object] = {
            "arguments": _arguments(spec),
            "origin": origin,
            "window": window,
            "summary": summary_counts,
        }
        if spec.browser and browser_setup is not None:
            run_data["browser"] = _browser_run_data(
                browser_setup, first_driver.observation, probe_records
            )
        record_problem = _write_json(
            host,
            spec.out / "run.json",
            run_data,
        )
        if record_problem is None:
            emit(StageResult("record", True, str(spec.out / "run.json"), ""))
        else:
            emit(StageResult("record", False, record_problem.problem, record_problem.fix))
    return int(
        not (
            bookkeeping.all_ok
            and cleanup_ok
            and requests_ok
            and record_problem is None
        )
    )


def run(
    spec: RunSpec,
    *,
    cases: Sequence[Case],
    origin: str = "",
    window: str = "",
    password: str,
    guardrail: Any,
    client_factory: Callable[..., Client],
    now: Callable[[], datetime],
    monotonic: Callable[[], float] = time.monotonic,
    host: Host | None = None,
    checkout: Path | None = None,
    browser_setup: BrowserSetup | None = None,
) -> int:
    """Sign in, run every repeated case, record it, and hand ownership back.

    The browser is closed and the output handed back whatever happened; a
    browser that does not close cleanly is a failed ``browser`` row and a
    non-zero exit, never a traceback.
    """

    io = host or RealHost()
    root = checkout or Path.cwd()
    output = spec.out
    output_ready = False
    code = 1
    try:
        if output is not None:
            io.mkdir(output, mode=0o755, parents=True, exist_ok=True)
            output_ready = True
        drivers: tuple[TurnDriver, ...]
        if spec.unfiltered:
            drivers = (
                UnfilteredTurnDriver(
                    client_factory,
                    password,
                    model=spec.model,
                    sentinel=spec.sentinel,
                ),
            )
        elif spec.browser:
            if browser_setup is None or output is None:
                print_stage(
                    StageResult(
                        "browser",
                        False,
                        "browser mode needs a launched page and --out",
                        "Run with --browser --out <dir>, then retry.",
                    )
                )
                return 1
            opened = browser.open_auth(browser_setup.page, browser_setup.hostname)
            if opened is not None:
                _emit_stage(
                    StageResult("browser", False, opened.problem, opened.fix),
                    host=io,
                    output=output,
                    browser_setup=browser_setup,
                )
                return 1
            _emit_stage(
                StageResult("browser", True, browser_setup.detail, ""),
                host=io,
                output=output,
                browser_setup=browser_setup,
            )
            drivers = (
                BrowserTurnDriver(
                    browser_setup,
                    client_factory,
                    password,
                    guardrail=guardrail,
                    out=output,
                ),
            )
        else:
            drivers = tuple(
                ApiTurnDriver(client_factory, password, model=spec.model)
                for _ in range(spec.concurrent)
            )
        code = _run_cases(
            spec,
            cases=cases,
            origin=origin,
            window=window,
            guardrail=guardrail,
            now=now,
            monotonic=monotonic,
            host=io,
            drivers=drivers,
            browser_setup=browser_setup,
        )
    finally:
        try:
            if browser_setup is not None:
                _write_requests_log(io, browser_setup, output)
                try:
                    browser_setup.close()
                except Exception as exc:  # noqa: BLE001 - teardown owns its traceback.
                    code = 1
                    print_stage(
                        StageResult(
                            "browser",
                            False,
                            f"the browser did not close cleanly: {type(exc).__name__}",
                            "Check for a leftover chromium process, then retry.",
                        )
                    )
        finally:
            if output_ready and output is not None:
                owner = sudo_ids()
                if owner is not None:
                    restore_ownership(io, output, owner, checkout=root)
    return code
