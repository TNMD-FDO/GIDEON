"""Write a committed reference file from one recorded evaluation run."""

import argparse
import subprocess
from collections.abc import Mapping
from pathlib import Path
from stat import S_ISDIR
from typing import Final

from gideon.evaluation import record, reference
from gideon.evaluation.command import git_argv, load_set_with_courts
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, print_findings
from gideon.evaluation.slices import SLICE_RUNNERS
from gideon.host import courts
from gideon.host.report import Problem, StageResult, print_stage
from gideon.host.sysio import Host, PathLike, RealHost

_READER_FIX: Final[str] = "Use a recorded run from the release's eval set, then retry."
_CLEAN_FIX: Final[str] = "Run the evaluation from a clean checkout at the release tag, then retry."
_OVERRIDES_FIX: Final[str] = "Run the evaluation without overrides, then retry."
# A suite-wide run names no slice, so it has no key under eval/reference/;
# slice-2 tickets 12 and 13 choose one when their suites land.
_SUITE_WIDE_FIX: Final[str] = (
    "Use a run of one slice; a suite-wide run has no reference key until "
    "slice-2 tickets 12 and 13 choose one."
)
_WRITE_FIX: Final[str] = "Restore the release checkout's reference directory ownership, then retry."
_UNSIGNED_FIX: Final[str] = (
    "Sign the case through the sign-off kit or re-run once the runner selects "
    "through the loader, then retry."
)
# The registry's compares_reference is the one switch: a reference eval run
# never reads would be a committed file nothing holds.
_NO_REFERENCE_FIX: Final[str] = (
    "Use a run of a slice that eval run compares against a reference, then retry."
)

type CheckResult = tuple[str, Mapping[str, reference.Verdict], reference.Comparison] | Problem


def _stage_refusal(stage: str, problem: Problem) -> int:
    print_stage(StageResult(stage, False, problem.problem, problem.fix))
    return 1


def _tag_fix(checkout: Path, product_version: str) -> str:
    tag = f"v{product_version}^{{commit}}"
    argv = git_argv(checkout, "rev-parse", "--verify", tag)
    return f"Run {' '.join(argv)} as the checkout owner, then retry."


def _check(
    loaded: LoadedSet,
    run: record.RecordedRun,
    *,
    checkout: Path,
    host: Host,
) -> CheckResult:
    if run.git_dirty is not False:
        return Problem("recorded run was not clean", _CLEAN_FIX)
    if run.git_sha is None:
        return Problem("recorded run has no git sha", _CLEAN_FIX)
    if run.overrides:
        return Problem("recorded run has overrides", _OVERRIDES_FIX)
    if run.slice is None:
        return Problem("recorded run names no slice", _SUITE_WIDE_FIX)
    if run.slice not in loaded.slices:
        return Problem(
            f"recorded run names slice {run.slice!r}, which the loaded eval set does not hold",
            _READER_FIX,
        )
    slice_spec = SLICE_RUNNERS.get(run.slice)
    if slice_spec is not None and not slice_spec.compares_reference:
        return Problem(f"slice {run.slice!r} keeps no reference", _NO_REFERENCE_FIX)
    if run.eval_set_version != loaded.version:
        return Problem(
            f"recorded run is for {run.eval_set_version}, not {loaded.version}",
            _READER_FIX,
        )
    if run.set_digest != loaded.digest:
        return Problem("recorded run has a different eval-set digest", _READER_FIX)

    tag = f"v{run.product_version}^{{commit}}"
    try:
        result = host.run(git_argv(checkout, "rev-parse", "--verify", tag))
    except (OSError, subprocess.SubprocessError):
        return Problem("product tag could not be resolved", _tag_fix(checkout, run.product_version))
    if result.returncode != 0:
        return Problem(
            f"product tag v{run.product_version} could not be resolved",
            _tag_fix(checkout, run.product_version),
        )
    if result.stdout.strip() != run.git_sha:
        return Problem(
            f"product tag v{run.product_version} does not name the recorded git sha",
            _tag_fix(checkout, run.product_version),
        )

    reference_result = reference.read_reference(
        checkout,
        run.slice,
        loaded.slice_lists[run.slice],
        loaded.version,
        host=host,
    )
    if reference_result.findings:
        print_findings(reference_result.findings)
        return Problem("slice reference is malformed", reference.SLICE_REPAIR_FIX)
    try:
        current = reference.fold_repeats(run.results)
    except ValueError as exc:
        return Problem(f"recorded run has invalid results ({exc})", _READER_FIX)
    unsigned_ids = tuple(
        dict.fromkeys(
            case_id
            for case_id, _repeat, _verdict in run.results
            if case_id in loaded.unsigned_ids
        )
    )
    if unsigned_ids:
        return Problem(
            f"recorded run names unsigned cases: {' '.join(unsigned_ids)}",
            _UNSIGNED_FIX,
        )
    comparison = reference.compare_reference(reference_result.reference, current, loaded.version)
    if comparison.outcome == "regressed":
        count = len(comparison.regressed)
        noun = "case" if count == 1 else "cases"
        return Problem(
            f"recorded run regresses {count} reference {noun}: {' '.join(comparison.regressed)}",
            reference.REGRESSION_FIX,
        )
    return run.slice, current, comparison


def _write(
    loaded: LoadedSet,
    run: record.RecordedRun,
    current: Mapping[str, reference.Verdict],
    comparison: reference.Comparison,
    *,
    slice_name: str,
    checkout: Path,
    host: Host,
) -> Problem | str:
    directory = checkout / reference.REFERENCE_ROOT / slice_name
    try:
        owner = host.stat(checkout)
        if not host.exists(directory):
            host.mkdir(directory, parents=True, exist_ok=True)
        elif not S_ISDIR(host.stat(directory).st_mode):
            return Problem("reference path is not a directory", _WRITE_FIX)
        # Ownership is handed back on every run, not only where bytes moved: a
        # run whose write succeeded and whose chown failed must be repaired by
        # the retry, never reported unchanged over root-owned files.
        host.chown(directory, owner.st_uid, owner.st_gid)

        changed = False
        for list_name in sorted(loaded.slice_lists[slice_name]):
            ids = loaded.slice_lists[slice_name][list_name]
            cases = {
                case_id: current[case_id]
                for case_id in ids
                if case_id in current
            }
            file = reference.ReferenceFile(
                format=reference.FORMAT_VERSION,
                product_version=run.product_version,
                corpus_lockfile=run.corpus_lockfile,
                eval_set_version=run.eval_set_version,
                hardware_profile=run.hardware_profile,
                tag=f"v{run.product_version}",
                slice=slice_name,
                list=list_name,
                repeats=run.repeats,
                set_digest=run.set_digest,
                cases=cases,
            )
            text = reference.serialize_reference(file)
            path = directory / f"{list_name}.json"
            unchanged = False
            if host.exists(path):
                try:
                    unchanged = host.read_text(path, encoding="utf-8") == text
                except (OSError, UnicodeError, ValueError):
                    unchanged = False
            if not unchanged:
                host.write_text(path, text, encoding="utf-8")
                changed = True
            host.chown(path, owner.st_uid, owner.st_gid)
    except (OSError, UnicodeError, ValueError) as exc:
        return Problem(f"reference files could not be written ({exc})", _WRITE_FIX)
    status = "written" if changed else "unchanged"
    return (
        f"gained {len(comparison.gained)}, new {len(comparison.new)}, "
        f"dropped {len(comparison.dropped)}; {status}"
    )


def run_reference(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    checkout_root: PathLike | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered",
    court_path: PathLike | None = None,
) -> int:
    """Read one recorded run and write its canonical reference files."""

    checkout = Path(__file__).parents[2] if checkout_root is None else Path(checkout_root)
    io = RealHost() if host is None else host
    selected_court_path = courts.default_courts_path() if court_path is None else Path(court_path)

    loaded_result = load_set_with_courts(
        checkout / SET_ROOT, court_path=selected_court_path, host=io
    )
    if loaded_result.findings:
        print_findings(loaded_result.findings)
        return _stage_refusal("load", Problem("eval set refused", "Correct every listed eval-set finding, then retry."))
    loaded = loaded_result.loaded
    if loaded is None:
        return _stage_refusal("load", Problem("eval set was not loaded", "Correct the eval set, then retry."))
    print_stage(
        StageResult(
            "load",
            True,
            f"{loaded.version}: {len(loaded.cases_by_id)} cases, digest {loaded.digest}",
            "",
        )
    )

    run_id = getattr(args, "run", None)
    if not isinstance(run_id, str) or not run_id:
        return _stage_refusal(
            "read",
            Problem("no recorded run id was supplied", "Supply --run <id>, then retry."),
        )
    run, read_problem = record.read_run(io, rendered_dir, run_id)
    if read_problem is not None:
        return _stage_refusal("read", read_problem)
    if run is None:
        return _stage_refusal(
            "read",
            Problem("recorded run was not read", "Use a recorded run id, then retry."),
        )
    print_stage(
        StageResult(
            "read",
            True,
            f"run {run.run_id} read ({len(run.results)} result rows)",
            "",
        )
    )

    checked = _check(loaded, run, checkout=checkout, host=io)
    if isinstance(checked, Problem):
        return _stage_refusal("check", checked)
    slice_name, current, comparison = checked
    print_stage(
        StageResult(
            "check",
            True,
            f"reference {comparison.outcome}; no regression",
            "",
        )
    )

    written = _write(
        loaded,
        run,
        current,
        comparison,
        slice_name=slice_name,
        checkout=checkout,
        host=io,
    )
    if isinstance(written, Problem):
        return _stage_refusal("write", written)
    print_stage(StageResult("write", True, written, ""))
    return 0
