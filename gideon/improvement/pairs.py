"""Read recorded challenger pairs and present figures without gating a run.

The pooled nights use the statistic in ``decision.py``.
Only identifiers and computed figures reach this section's rows.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from gideon.evaluation import challenger, decision
from gideon.host.report import Problem
from gideon.improvement.ratings import TIME_FORMAT
from gideon.improvement.sections import (
    READ_FIX,
    Context,
    Row,
    RowState,
    Scope,
    SectionReport,
)

SECTION_NAME: Final[str] = "challenger"
POOLED_NIGHTS: Final[int] = 5
FIGURE_NAME: Final[str] = "in_band"
IN_BAND_METRIC: Final[decision.DecisionMetric] = decision.DecisionMetric(
    FIGURE_NAME, True, lambda _metrics: None
)
NEWEST_ROW: Final[str] = "newest"
POOLED_ROW: Final[str] = "pooled"
PARSE_PAIRS_PROBLEM: Final[str] = "metrics reader returned {count} unreadable pair rows"
PARSE_RESULTS_PROBLEM: Final[str] = (
    "metrics reader returned {count} unreadable result rows"
)
HEADER_DETAIL: Final[str] = (
    "{name}, {subject}, release {release}, challenger {challenger}, "
    "{nights} of {depth} nights pooled"
)
NO_PAIR_DETAIL: Final[str] = "no pair recorded"
NO_NIGHTS_DETAIL: Final[str] = "0 of {depth} nights"
PARTNERLESS_DETAIL: Final[str] = (
    "release {run_id}, value {value}, kind {kind}, start {start}; "
    "no challenger row pairs it{partial}"
)
SIDE_DETAIL: Final[str] = (
    "{side} {run_id}, value {value}, in band {in_band} of {cases} cases, "
    "gradings {gradings}, failed {failed}, prompt tokens {prompt_tokens}, "
    "completion tokens {completion_tokens}"
)
NEWEST_DETAIL: Final[str] = "{release}; {challenger}; kind {kind}, start {start}"
POOLED_DETAIL: Final[str] = "in_band over {nights} of {depth} nights: {decision}"

_UUID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_TIME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*")
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./@-]*")
_DECIMAL_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9]+")
# The run row's kind vocabulary, the check constraint on eval_runs.kind.
_KINDS: Final[frozenset[str]] = frozenset(
    {
        "smoke",
        "nightly",
        "weekly-off",
        "decision",
        "candidate",
        "engine-verify",
        "manual",
    }
)

PAIRS_STATEMENT: Final[str] = (
    "SELECT release_run.run_id, "
    "to_char(release_run.started_at AT TIME ZONE 'UTC', "
    '\'YYYY-MM-DD"T"HH24:MI:SS"Z"\'), '
    "release_run.kind, release_run.product_version, "
    "release_run.eval_set_version, release_run.set_digest, "
    "release_run.repeats, release_run.partial, "
    f"release_run.overrides->'{challenger.OVERRIDE_KEY}'->>'{challenger.VALUE_FIELD}', "
    "partner.run_id, partner.partial, partner.value "
    "FROM eval_runs AS release_run "
    "LEFT JOIN LATERAL ("
    "SELECT candidate.run_id, candidate.partial, "
    f"candidate.overrides->'{challenger.OVERRIDE_KEY}'->>'{challenger.VALUE_FIELD}' AS value "
    "FROM eval_runs AS candidate "
    "WHERE candidate.stack = 'ci' "
    f"AND candidate.overrides->'{challenger.OVERRIDE_KEY}'->>'{challenger.NAME_FIELD}' = '{{name}}' "
    f"AND candidate.overrides->'{challenger.OVERRIDE_KEY}'->>'{challenger.SIDE_FIELD}' = '{challenger.CHALLENGER_SIDE}' "
    f"AND candidate.overrides->'{challenger.OVERRIDE_KEY}'->>'{challenger.PAIRS_FIELD}' = release_run.run_id::text "
    "ORDER BY candidate.started_at DESC, candidate.run_id DESC LIMIT 1"
    ") AS partner ON true "
    "WHERE release_run.stack = 'ci' "
    f"AND release_run.overrides->'{challenger.OVERRIDE_KEY}'->>'{challenger.NAME_FIELD}' = '{{name}}' "
    f"AND release_run.overrides->'{challenger.OVERRIDE_KEY}'->>'{challenger.SIDE_FIELD}' = '{challenger.RELEASE_SIDE}' "
    "ORDER BY release_run.started_at DESC, release_run.run_id DESC"
)
RESULTS_STATEMENT: Final[str] = (
    "SELECT run_id, case_id, repeat, "
    f"judge->>'{FIGURE_NAME}', judge ? 'failed', "
    "metrics->>'prompt_tokens', metrics->>'completion_tokens' "
    "FROM eval_results WHERE run_id IN ({run_ids}) "
    "ORDER BY run_id, case_id, repeat"
)


@dataclass(frozen=True, slots=True)
class PairRow:
    """A release run and its newest recorded challenger partner, if present."""

    release_id: str
    start: str
    kind: str
    product_version: str
    set_version: str
    set_digest: str
    repeats: int
    release_partial: bool
    release_value: str
    partner_id: str | None
    partner_partial: bool | None
    partner_value: str | None


@dataclass(frozen=True, slots=True)
class ResultRow:
    """One grading's content-free figure and token counts."""

    run_id: str
    case_id: str
    repeat: int
    in_band: bool | None
    failed: bool
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True, slots=True)
class NightFigures:
    """Case and grading totals for one side of one night."""

    cases: int
    cases_in_band: int
    gradings: int
    failed: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class Selection:
    """The newest release run and up to five complete nights from its version."""

    newest: PairRow | None
    pooled: tuple[PairRow, ...]


def pairs_statement(name: str) -> str:
    """Read release rows under a validated committed name and their newest partner."""

    if challenger.NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("challenger name must match the loader's name grammar")
    return PAIRS_STATEMENT.format(name=name)


def results_statement(run_ids: tuple[str, ...]) -> str:
    """Read selected run results using validated UUID literals."""

    ids: list[str] = []
    for run_id in sorted(set(run_ids)):
        if not _uuid(run_id):
            raise ValueError("run id must be a UUID")
        ids.append(f"'{UUID(run_id)}'::uuid")
    if not ids:
        raise ValueError("at least one run id is required")
    return RESULTS_STATEMENT.format(run_ids=", ".join(ids))


def _uuid(value: str) -> bool:
    if _UUID_PATTERN.fullmatch(value) is None:
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _decimal(value: str, *, minimum: int = 0) -> int | None:
    if _DECIMAL_PATTERN.fullmatch(value) is None:
        return None
    number = int(value)
    return number if number >= minimum else None


def _valid_time(value: str) -> bool:
    if _TIME_PATTERN.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.strftime(TIME_FORMAT) == value


def parse_pairs(lines: tuple[str, ...]) -> tuple[PairRow, ...] | Problem:
    """Accept only content-free pair columns; count unreadable rows without quoting."""

    parsed: list[PairRow] = []
    unreadable = 0
    for line in lines:
        columns = line.split("|")
        if len(columns) != 12:
            unreadable += 1
            continue
        (
            release_id,
            start,
            kind,
            product_version,
            set_version,
            set_digest,
            repeats_text,
            release_partial,
            release_value,
            partner_id,
            partner_partial,
            partner_value,
        ) = columns
        repeats = _decimal(repeats_text, minimum=1)
        partner_present = bool(partner_id)
        if (
            not _uuid(release_id)
            or not _valid_time(start)
            or kind not in _KINDS
            or _VERSION_PATTERN.fullmatch(product_version) is None
            or _VERSION_PATTERN.fullmatch(set_version) is None
            or _DIGEST_PATTERN.fullmatch(set_digest) is None
            or repeats is None
            or release_partial not in ("t", "f")
            or _ID_PATTERN.fullmatch(release_value) is None
            or (partner_present and not _uuid(partner_id))
            or (partner_present and partner_partial not in ("t", "f"))
            or (partner_present and _ID_PATTERN.fullmatch(partner_value) is None)
            or (not partner_present and (partner_partial or partner_value))
        ):
            unreadable += 1
            continue
        parsed.append(
            PairRow(
                release_id,
                start,
                kind,
                product_version,
                set_version,
                set_digest,
                repeats,
                release_partial == "t",
                release_value,
                partner_id or None,
                partner_partial == "t" if partner_present else None,
                partner_value or None,
            )
        )
    if unreadable:
        return Problem(PARSE_PAIRS_PROBLEM.format(count=unreadable), READ_FIX)
    return tuple(parsed)


def parse_results(lines: tuple[str, ...]) -> tuple[ResultRow, ...] | Problem:
    """Accept figures and token counts, with undefined failed gradings distinct."""

    parsed: list[ResultRow] = []
    unreadable = 0
    for line in lines:
        columns = line.split("|")
        if len(columns) != 7:
            unreadable += 1
            continue
        (
            run_id,
            case_id,
            repeat_text,
            in_band_text,
            failed_text,
            prompt_text,
            completion_text,
        ) = columns
        repeat = _decimal(repeat_text, minimum=1)
        prompt = _decimal(prompt_text) if prompt_text else None
        completion = _decimal(completion_text) if completion_text else None
        if (
            not _uuid(run_id)
            or _ID_PATTERN.fullmatch(case_id) is None
            or repeat is None
            or in_band_text not in ("true", "false", "")
            or failed_text not in ("t", "f")
            or (failed_text == "t") != (in_band_text == "")
            or (bool(prompt_text) and prompt is None)
            or (bool(completion_text) and completion is None)
        ):
            unreadable += 1
            continue
        parsed.append(
            ResultRow(
                run_id,
                case_id,
                repeat,
                None if not in_band_text else in_band_text == "true",
                failed_text == "t",
                prompt,
                completion,
            )
        )
    if unreadable:
        return Problem(PARSE_RESULTS_PROBLEM.format(count=unreadable), READ_FIX)
    return tuple(parsed)


def select_pairs(pairs: tuple[PairRow, ...]) -> Selection:
    """Keep complete pairs from the newest row's release and eval-set versions."""

    newest = pairs[0] if pairs else None
    if newest is None:
        return Selection(None, ())
    pooled = tuple(
        pair
        for pair in pairs
        if pair.partner_id is not None
        and not pair.release_partial
        and pair.partner_partial is False
        and pair.product_version == newest.product_version
        and pair.set_version == newest.set_version
    )[:POOLED_NIGHTS]
    return Selection(newest, pooled)


def night_figures(results: tuple[ResultRow, ...], repeats: int) -> NightFigures:
    """Count complete in-band cases and sum the recorded grading figures."""

    by_case: dict[str, list[ResultRow]] = {}
    for result in results:
        by_case.setdefault(result.case_id, []).append(result)
    in_band = sum(
        len(rows) == repeats
        and {row.repeat for row in rows} == set(range(1, repeats + 1))
        and all(row.in_band is True for row in rows)
        for rows in by_case.values()
    )
    return NightFigures(
        len(by_case),
        in_band,
        len(results),
        sum(row.failed for row in results),
        sum(row.prompt_tokens or 0 for row in results),
        sum(row.completion_tokens or 0 for row in results),
    )


def pooled_decision(
    pairs: tuple[PairRow, ...], results: tuple[ResultRow, ...], *, against: str
) -> decision.PairedDecision:
    """Compare per-case in-band means across the selected complete nights."""

    by_run: dict[str, tuple[ResultRow, ...]] = {
        run_id: tuple(row for row in results if row.run_id == run_id)
        for pair in pairs
        for run_id in (pair.release_id, pair.partner_id)
        if run_id is not None
    }
    release: dict[str, list[float]] = {}
    candidate: dict[str, list[float]] = {}
    for pair in pairs:
        for target, run_id in (
            (release, pair.release_id),
            (candidate, pair.partner_id),
        ):
            if run_id is None:
                continue
            for row in by_run[run_id]:
                if row.in_band is not None:
                    target.setdefault(row.case_id, []).append(float(row.in_band))
    release_values = {case_id: tuple(values) for case_id, values in release.items()}
    candidate_values = {case_id: tuple(values) for case_id, values in candidate.items()}
    clusters = {
        case_id: case_id for case_id in release_values.keys() | candidate_values.keys()
    }
    return decision.paired_decision(
        candidate_values,
        release_values,
        clusters,
        IN_BAND_METRIC,
        against=against,
        requested_repeats=POOLED_NIGHTS,
        completed_repeats=len(pairs),
        digest_equal=len({pair.set_digest for pair in pairs}) == 1,
    )


def _side_detail(side: str, run_id: str, value: str, figures: NightFigures) -> str:
    return SIDE_DETAIL.format(
        side=side,
        run_id=run_id,
        value=value,
        in_band=figures.cases_in_band,
        cases=figures.cases,
        gradings=figures.gradings,
        failed=figures.failed,
        prompt_tokens=figures.prompt_tokens,
        completion_tokens=figures.completion_tokens,
    )


def _pooled_row(selection: Selection, results: tuple[ResultRow, ...]) -> Row:
    if not selection.pooled:
        return Row(
            POOLED_ROW,
            "not yet measurable",
            NO_NIGHTS_DETAIL.format(depth=POOLED_NIGHTS),
        )
    newest = selection.newest
    assert newest is not None
    signal = pooled_decision(selection.pooled, results, against=newest.release_value)
    detail = POOLED_DETAIL.format(
        nights=len(selection.pooled),
        depth=POOLED_NIGHTS,
        decision=decision.describe(signal),
    )
    if not signal.digest_equal:
        detail += "; digest differs"
    state: RowState = (
        "not yet measurable"
        if signal.paired < 2
        else "fired"
        if signal.verdict == decision.WINS
        else "not fired"
    )
    return Row(POOLED_ROW, state, detail)


@dataclass(frozen=True, slots=True)
class ChallengerSection:
    """Read recorded pairs and report the newest and pooled measurements."""

    name: str = SECTION_NAME
    scope: Scope = "product"

    def render(self, context: Context) -> SectionReport | Problem:
        """Load the committed experiment, then read its content-free run figures."""

        loaded = challenger.load_challenger(
            context.checkout_root / challenger.CHALLENGER_PATH, host=context.host
        )
        if loaded.findings:
            first = loaded.findings[0]
            return Problem(
                f"challenger file has {len(loaded.findings)} findings: "
                f"{first.key_path}: {first.problem}",
                first.fix,
            )
        if loaded.config is None:
            return Problem("challenger file could not be loaded", READ_FIX)
        entry = loaded.config.challenger
        if entry is None:
            return SectionReport("none set", (Row(NEWEST_ROW, "skipped", "none set"),))

        pair_lines = context.query(pairs_statement(entry.name))
        if isinstance(pair_lines, Problem):
            return pair_lines
        pairs = parse_pairs(pair_lines)
        if isinstance(pairs, Problem):
            return pairs
        selection = select_pairs(pairs)
        header = HEADER_DETAIL.format(
            name=entry.name,
            subject=entry.subject,
            release=entry.release,
            challenger=entry.challenger,
            nights=len(selection.pooled),
            depth=POOLED_NIGHTS,
        )
        newest = selection.newest
        if newest is None:
            return SectionReport(
                header,
                (
                    Row(NEWEST_ROW, "not yet measurable", NO_PAIR_DETAIL),
                    _pooled_row(selection, ()),
                ),
            )

        run_ids = {
            run_id
            for pair in (newest, *selection.pooled)
            for run_id in (pair.release_id, pair.partner_id)
            if run_id is not None
        }
        results: tuple[ResultRow, ...] = ()
        if newest.partner_id is not None or selection.pooled:
            result_lines = context.query(results_statement(tuple(run_ids)))
            if isinstance(result_lines, Problem):
                return result_lines
            parsed = parse_results(result_lines)
            if isinstance(parsed, Problem):
                return parsed
            results = parsed

        if newest.partner_id is None:
            newest_row = Row(
                NEWEST_ROW,
                "not yet measurable",
                PARTNERLESS_DETAIL.format(
                    run_id=newest.release_id,
                    value=newest.release_value,
                    kind=newest.kind,
                    start=newest.start,
                    partial=", partial" if newest.release_partial else "",
                ),
            )
        else:
            release_figures = night_figures(
                tuple(row for row in results if row.run_id == newest.release_id),
                newest.repeats,
            )
            partner_figures = night_figures(
                tuple(row for row in results if row.run_id == newest.partner_id),
                newest.repeats,
            )
            newest_row = Row(
                NEWEST_ROW,
                "not fired",
                NEWEST_DETAIL.format(
                    release=_side_detail(
                        challenger.RELEASE_SIDE,
                        newest.release_id,
                        newest.release_value,
                        release_figures,
                    ),
                    challenger=_side_detail(
                        challenger.CHALLENGER_SIDE,
                        newest.partner_id,
                        newest.partner_value or "",
                        partner_figures,
                    ),
                    kind=newest.kind,
                    start=newest.start,
                ),
            )
        return SectionReport(header, (newest_row, _pooled_row(selection, results)))


CHALLENGER_SECTION: Final[ChallengerSection] = ChallengerSection()
