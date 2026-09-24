"""Run General's smoke set as managed turns through the frontend.

§18.2's ``general-smoke`` suite: every active case is one managed turn as the
eval identity through Open WebUI's chat path, the frontend's adapter the thing
under test (ADR-0045), replayed on the raw streaming route and judged by the
turn harness's own checks over the stored record. A turn passes iff the
harness's row holds — its expectation, ``must``, ``must_not``, ``block``,
``sources``, ``withheld``, and a clean stream — and its chat was identified and
deleted; nothing is graded (ADR-0006, ADR-0023). Rows carry ids, classes,
check names, and fixed codes, never text.
"""

import re
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from gideon import guardrail
from gideon.evaluation.evalset import Case as EvalCase
from gideon.evaluation.evalset import LoadedSet, select_cases
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult
from gideon.evaluation.turns import cases, run
from gideon.host import owui, owuiturn
from gideon.host.render.owui import EVAL_IDENTITY, GENERAL_PRESET_ID

PROBLEMS: Final[frozenset[str]] = frozenset(
    {
        "turns-unavailable",
        "frontend-signin",
        "turn-error",
        "unverified",
        "cleanup-failed",
        "stream-error",
        "stream-leak",
        "check-failed",
    }
)
"""The closed codes of a turn that failed."""
_CASES_PATH: Final[Path] = Path("general")
_CLEANUP: Final[frozenset[str]] = frozenset({"unverified", "cleanup-failed"})


@dataclass(frozen=True, slots=True)
class _Outcome:
    """One turn's facts: a case at one repeat."""

    case_id: str
    repeat: int
    expected: str
    search: bool
    class_name: str | None = None
    stream: str | None = None
    pattern: str | None = None
    checks: Mapping[str, bool] = field(default_factory=dict)
    failed: tuple[str, ...] = ()
    elapsed: float | None = None
    problem: str | None = None

    @property
    def passed(self) -> bool:
        return self.problem is None


def _expected(record: EvalCase) -> dict[str, object]:
    return cast(dict[str, object], record["expected"])


def _base(record: EvalCase, repeat: int) -> _Outcome:
    expected = _expected(record)
    return _Outcome(
        cast(str, record["id"]),
        repeat,
        cast(str, expected["expect"]),
        cast(bool, expected["search"]),
    )


def _turn_case(record: EvalCase) -> cases.Case:
    expected = _expected(record)
    return cases.Case(
        cast(str, record["id"]),
        cast(str, record["question"]),
        cast(str, expected["expect"]),
        tuple(re.compile(pattern) for pattern in cast(list[str], expected["must"])),
        tuple(re.compile(pattern) for pattern in cast(list[str], expected["must_not"])),
        cast(str, expected["block"]),
        search=cast(bool, expected["search"]),
        sources=cast(str, expected["sources"]),
    )


def _turn_outcome(record: EvalCase, repeat: int, row: run.TurnRow) -> _Outcome:
    """The per-turn verdict: code over the row's facts, cleanup first.

    The harness's loop reads cleanup in its own row; a caller of the per-turn
    unit reads it here, so a chat left behind fails its turn.
    """

    failed = tuple(name for name, ok in row.checks.items() if not ok)
    problem: str | None
    if row.chat_id is None:
        problem = "unverified"
    elif not row.deleted:
        problem = "cleanup-failed"
    elif row.verdict_kind is None:
        problem = "turn-error"
    elif row.stream_kind == "leak":
        problem = "stream-leak"
    elif row.stream_kind != "clean":
        problem = "stream-error"
    elif failed:
        problem = "check-failed"
    else:
        problem = None
    base = _base(record, repeat)
    return _Outcome(
        base.case_id,
        repeat,
        base.expected,
        base.search,
        class_name=row.verdict_kind,
        stream=row.stream_kind,
        pattern=row.pattern_id or row.stream_pattern_id,
        checks=dict(row.checks),
        failed=failed,
        elapsed=row.elapsed,
        problem=problem,
    )


def _metrics(outcome: _Outcome) -> dict[str, JSONValue]:
    metrics: dict[str, JSONValue] = {"expect": outcome.expected, "search": outcome.search}
    if outcome.class_name is not None:
        metrics["class"] = outcome.class_name
    if outcome.stream is not None:
        metrics["stream"] = outcome.stream
    if outcome.pattern is not None:
        metrics["pattern"] = outcome.pattern
    if outcome.checks:
        metrics["checks"] = dict(outcome.checks)
    if outcome.problem is not None:
        assert outcome.problem in PROBLEMS, outcome.problem
        metrics["problem"] = outcome.problem
    if outcome.failed:
        metrics["failed"] = list(outcome.failed)
    return metrics


def _result(outcome: _Outcome) -> CaseResult:
    return CaseResult(
        outcome.case_id,
        outcome.repeat,
        "pass" if outcome.passed else "fail",
        _metrics(outcome),
        latency_ms=None if outcome.elapsed is None else outcome.elapsed * 1000,
    )


def _progress(outcome: _Outcome) -> str:
    checks = ", ".join(outcome.failed) or ("ok" if outcome.checks else "none")
    seconds = "unknown" if outcome.elapsed is None else f"{outcome.elapsed:.2f}"
    return (
        f"general-smoke {outcome.case_id}#{outcome.repeat}: "
        f"{outcome.class_name or 'error'}; checks {checks}; "
        f"stream {outcome.stream or 'none'}; seconds {seconds}"
    )


def _ids(values: tuple[str, ...]) -> str:
    return ", ".join(values) or "none"


def _case_line(case_id: str, rows: tuple[_Outcome, ...]) -> str:
    details = []
    for row in rows:
        detail = f"repeat {row.repeat} {row.class_name or 'error'}"
        if row.problem is not None:
            detail += f", {row.problem}"
            if row.failed:
                detail += f": {', '.join(row.failed)}"
        details.append(detail)
    verdict = "pass" if all(row.passed for row in rows) else "fail"
    return f"{case_id}: {verdict} — " + "; ".join(details)


def _slice_result(
    outcomes: tuple[_Outcome, ...],
    case_ids: tuple[str, ...],
    repeats: int,
    head: tuple[str, ...],
    *,
    chats_deleted: int = 0,
    chat_count: str = "unknown",
) -> SliceResult:
    """The report and the gate: every turn passed."""

    by_case = {
        case_id: tuple(row for row in outcomes if row.case_id == case_id) for case_id in case_ids
    }
    turns_made = sum(row.problem not in {"turns-unavailable", "frontend-signin"} for row in outcomes)
    lines = [*head, *(_case_line(case_id, by_case[case_id]) for case_id in case_ids)]
    lines.append(
        f"chats: {turns_made} turns made, {chats_deleted} chats deleted; "
        f"the eval identity's chats at the end: {chat_count}"
    )
    if any(row.problem in _CLEANUP for row in outcomes):
        lines.append(f"Fix: {run.unverified_fix(EVAL_IDENTITY.username)}")
    classes = Counter(row.class_name for row in outcomes if row.class_name is not None)
    class_detail = ", ".join(f"{name} {classes[name]}" for name in sorted(classes)) or "none"
    failed_ids = tuple(
        case_id for case_id in case_ids if not all(row.passed for row in by_case[case_id])
    )
    passed = bool(outcomes) and all(row.passed for row in outcomes)
    lines.append(
        f"general-smoke: {'pass' if passed else 'fail'}; {len(case_ids)} cases at "
        f"{repeats} repeats, {turns_made} turns; classes {class_detail}; "
        f"failed: {_ids(failed_ids)}"
    )
    report = "\n".join(lines) + "\n"
    return SliceResult(passed, report, tuple(_result(row) for row in outcomes))


def _now() -> datetime:
    return datetime.now(UTC)


def run_general_smoke(eval_set: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Run the active cases repeat-major in id order, and gate every turn in code."""

    selected = select_cases(eval_set, slice_name)
    records = tuple(eval_set.cases_by_id[case_id] for case_id in selected.counted)
    case_ids = tuple(cast(str, record["id"]) for record in records)
    repeats = range(1, context.repeats + 1)

    def failed_all(problem: str, head: tuple[str, ...]) -> SliceResult:
        outcomes = tuple(
            replace(_base(record, repeat), problem=problem)
            for repeat in repeats
            for record in records
        )
        for outcome in outcomes:
            context.progress(_progress(outcome))
        return _slice_result(outcomes, case_ids, context.repeats, head)

    turns = context.turns
    if turns is None:
        # The tripwires' construction: no host call, one failed row per turn.
        return failed_all("turns-unavailable", ("turn access unavailable: the command supplies it",))

    driver = run.ApiTurnDriver(turns.client_factory, turns.password)
    try:
        signin_detail = driver.signin()
    except owui.OwuiError as exc:
        return failed_all("frontend-signin", (f"frontend signin: {exc.problem}", f"Fix: {exc.fix}"))

    spec = run.RunSpec(
        cases=_CASES_PATH,
        repeat=context.repeats,
        stream=True,
        out=None,
        force=False,
        dry_run=False,
        sentinel=turns.sentinel,
        model=GENERAL_PRESET_ID,
        stack="production",
    )
    collected: list[_Outcome] = []
    chats_deleted = 0
    for repeat in repeats:
        for record in records:
            case = _turn_case(record)
            row = run.frontend_turn(
                spec,
                client=driver.client,
                driver=driver,
                guardrail=guardrail,
                case=case,
                session_number=1,
                row_name=f"{case.id}#{repeat}",
                now=_now,
                monotonic=time.monotonic,
            )
            outcome = _turn_outcome(record, repeat, row)
            collected.append(outcome)
            chats_deleted += row.deleted
            context.progress(_progress(outcome))

    # Reported and never gated: a chat another session holds is a person's.
    try:
        chat_count = str(len(owuiturn.chat_ids(driver.client)))
    except owui.OwuiError:
        chat_count = "unknown"
    return _slice_result(
        tuple(collected),
        case_ids,
        context.repeats,
        (f"frontend signin: {signin_detail}",),
        chats_deleted=chats_deleted,
        chat_count=chat_count,
    )
