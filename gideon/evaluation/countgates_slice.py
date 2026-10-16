"""Judge per-court completeness and settled dispositions in a build report."""

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.evaluation import record
from gideon.evaluation.evalset import LoadedSet, select_cases
from gideon.evaluation.results import CaseResult, RunContext, SliceResult
from gideon.host import report, stack
from gideon.host.corpus.lockfile import load_lockfile
from gideon.host.index import buildreport
from gideon.host.index.sample import SAMPLE_SUFFIX
from gideon.host.report import Problem
from gideon.host.worker import bind

PROBLEMS: Final[frozenset[str]] = frozenset({
    "generation-unresolved", "record-unreachable", "report-absent", "report-invalid",
    "lockfile-absent", "lockfile-invalid", "unknown-rule",
})
LOCKFILE_DIRECTORY: Final[Path] = Path("corpus", "lockfiles")


@dataclass(frozen=True, slots=True)
class CourtFigures:
    """One court's staged count, recorded total, and disposition counts."""

    staged: int | None
    recorded: int
    ready: int
    failed: int
    named: int
    withdrawn: int
    processing: int


@dataclass(frozen=True, slots=True)
class _GateProblem(Problem):
    """A refusal with the closed code recorded on affected cases."""

    code: str


def court_figures(build_report: buildreport.BuildReport) -> Mapping[str, CourtFigures]:
    """Read every reported court, retaining a missing staged count as none."""

    accounted = build_report.gates.get("accounted", {})
    figures: dict[str, CourtFigures] = {}
    for court in sorted(set(build_report.courts) | set(accounted)):
        entry = build_report.courts.get(court)
        row = accounted.get(court)
        statuses = entry.counts.by_status if entry is not None else {}
        figures[court] = CourtFigures(
            staged=row.staged if row is not None else None,
            recorded=(
                row.recorded if row is not None
                else entry.counts.opinions if entry is not None else 0
            ),
            ready=statuses.get("ready", 0),
            failed=statuses.get("failed", 0),
            named=sum(entry.counts.by_failure_reason.values()) if entry is not None else 0,
            withdrawn=statuses.get("withdrawn", 0),
            processing=statuses.get("processing", 0),
        )
    return figures


def judge_staged(
    lockfile_courts: tuple[str, ...], figures: Mapping[str, CourtFigures],
) -> tuple[str, ...]:
    """Fail a court missing from either side or lacking its staged count."""

    courts = set(lockfile_courts)
    return tuple(sorted(
        court for court in courts | set(figures)
        if court not in courts or court not in figures or figures[court].staged is None
    ))


def judge_recorded(figures: Mapping[str, CourtFigures]) -> tuple[str, ...]:
    """Fail a court without a staged count or with fewer recorded opinions."""

    return tuple(sorted(
        court for court, counts in figures.items()
        if counts.staged is None or counts.recorded < counts.staged
    ))


def judge_settled(figures: Mapping[str, CourtFigures]) -> tuple[str, ...]:
    """Fail a court still processing documents or lacking a reason for each failure."""

    return tuple(sorted(
        court for court, counts in figures.items()
        if counts.processing > 0 or counts.named != counts.failed
    ))


RULES: Final[Mapping[str, Callable[..., tuple[str, ...]]]] = {
    "count-gates-staged": judge_staged,
    "count-gates-recorded": judge_recorded,
    "count-gates-settled": judge_settled,
}


def _read_report(name: str, context: RunContext) -> buildreport.BuildReport | None | _GateProblem:
    """Read the generation's newest build report through the selected stack's reader."""

    sql = bind("v_name", name) + "\n" + buildreport.READ_REPORT_SQL
    fix = f"Run {stack.logs_fix(context.turns_dir, record.POSTGRES_SERVICE)}, then retry."
    try:
        result = context.host.run(record.reader_argv(context.turns_dir), input=sql)
    except (OSError, subprocess.SubprocessError):
        return _GateProblem("build report read could not run", fix, "record-unreachable")
    if result.returncode != 0:
        return _GateProblem(
            f"build report read failed (exit {result.returncode})", fix, "record-unreachable",
        )
    if not result.stdout.strip():
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _GateProblem("build report row is invalid", fix, "report-invalid")
    parsed = buildreport.BuildReport.from_json(value)
    if parsed is None or parsed.gen_id != name:
        return _GateProblem("build report row is invalid", fix, "report-invalid")
    return parsed


def _read_courts(label: str, context: RunContext) -> tuple[str, ...] | _GateProblem:
    """Load the generation's committed lockfile courts from the run's checkout."""

    relative = LOCKFILE_DIRECTORY / f"{label}.yaml"
    fix = (
        f"Run the gate from a checkout holding {relative} "
        f"({report.command('corpus cut')} writes it; a scratch cut is no candidate), "
        "or name a generation of a committed lockfile with --generation <name>, then retry."
    )
    if context.checkout is None:
        return _GateProblem("the lockfile checkout is absent", fix, "lockfile-absent")
    path = Path(context.checkout) / relative
    if not path.is_file():
        return _GateProblem("the generation's lockfile is absent", fix, "lockfile-absent")
    loaded = load_lockfile(path)
    if loaded.errors or loaded.lockfile is None:
        return _GateProblem(
            f"lockfile errors {len(loaded.errors)}",
            f"Run {report.command('corpus cut')} to write a valid committed lockfile "
            "and its companion files, then retry.",
            "lockfile-invalid",
        )
    return loaded.lockfile.courts


def _failed(
    counted: tuple[str, ...], unsigned: int, code: str, problem: Problem,
) -> SliceResult:
    """Fail every counted case and render the content-free problem and its fix."""

    results = tuple(CaseResult(case_id, 1, "fail", {"problem": code}) for case_id in counted)
    lines = (
        f"{len(counted)} counted, {unsigned} unsigned excluded",
        *report.failure_lines(problem.fix, f"{code}: {problem.problem}"),
        "count-gates: fail",
    )
    return SliceResult(False, "\n".join(lines) + "\n", results)


def run_count_gates(loaded: LoadedSet, slice_name: str, context: RunContext) -> SliceResult:
    """Judge every selected rule against the resolved generation's build report."""

    selection = select_cases(loaded, slice_name)
    generation = context.generation
    if generation is None:
        return _failed(selection.counted, len(selection.unsigned), "generation-unresolved", Problem(
            "the stores generation was not resolved",
            f"Run {report.command(f'eval run --slice {slice_name} --generation <name>')} "
            "through the command's stores preconditions, then retry.",
        ))
    context.checkpoint()
    build_report = _read_report(generation.name, context)
    if isinstance(build_report, _GateProblem):
        return _failed(
            selection.counted, len(selection.unsigned), build_report.code, build_report,
        )
    if build_report is None:
        collection = generation.name.split("__", 1)[0]
        sample = generation.name.endswith(f"__{SAMPLE_SUFFIX}")
        build = f"index build {collection}" + (" --sample" if sample else "")
        build += f" --lockfile {generation.corpus_lockfile}"
        fix = (
            f"Run {report.command(build)} on the sibling once its build writes a report, then retry."
            if sample else
            f"Run {report.command(build)} so the build writes its report, then retry."
        )
        return _failed(selection.counted, len(selection.unsigned), "report-absent", Problem(
            "the generation has no build report", fix,
        ))

    figures = court_figures(build_report)
    lockfile_courts = _read_courts(generation.corpus_lockfile, context)
    lockfile_count = (
        "none" if isinstance(lockfile_courts, _GateProblem) else str(len(lockfile_courts))
    )
    lines = [
        f"generation {generation.name} ({generation.corpus_lockfile}, {generation.state})",
        f"report written_at {build_report.written_at}, state {build_report.state}, "
        f"failed_stage {build_report.failed_stage or 'none'}",
        f"courts {len(figures)} in the report, {lockfile_count} in the lockfile",
    ]
    for court, counts in figures.items():
        staged = "none" if counts.staged is None else str(counts.staged)
        lines.append(
            f"{court}: staged {staged}, recorded {counts.recorded}, ready {counts.ready}, "
            f"failed {counts.failed} (named {counts.named}), "
            f"withdrawn {counts.withdrawn}, processing {counts.processing}"
        )

    results: list[CaseResult] = []
    for case_id in selection.counted:
        context.checkpoint()
        # A rule id ends in its two-digit number; the rest is its series.
        judge = RULES.get(case_id.rsplit("-", 1)[0])
        problem: _GateProblem | None = None
        if judge is None:
            problem = _GateProblem(
                "the rule series has no judge",
                "Add the rule series and its judge to gideon/evaluation/countgates_slice.py "
                "with the cases in eval/sets/eval-v1/build-gates/count-gates.jsonl "
                "in the same release, then retry.",
                "unknown-rule",
            )
        elif judge is judge_staged:
            if isinstance(lockfile_courts, _GateProblem):
                problem = lockfile_courts
            else:
                failing = judge_staged(lockfile_courts, figures)
                courts = len(set(lockfile_courts) | set(figures))
        else:
            failing = judge(figures)
            courts = len(figures)
        if problem is not None:
            results.append(CaseResult(case_id, 1, "fail", {"problem": problem.code}))
            lines.append(f"{case_id}: fail ({problem.code})")
            lines.extend(report.failure_lines(problem.fix, f"{problem.code}: {problem.problem}"))
            continue
        verdict = "fail" if failing else "pass"
        results.append(CaseResult(case_id, 1, verdict, {"failing": list(failing), "courts": courts}))
        lines.append(f"{case_id}: {verdict}, {' '.join(failing) or 'none'}")

    passed = bool(results) and all(result.verdict == "pass" for result in results)
    lines.append(f"count-gates: {'pass' if passed else 'fail'}")
    return SliceResult(passed, "\n".join(lines) + "\n", tuple(results))
