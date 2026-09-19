"""The ordered host command for loading, running, and gating an eval slice."""

import argparse
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

import gideon
from gideon.evaluation import record
from gideon.evaluation.evalset import (
    SET_ROOT,
    EvalSetLoadResult,
    Finding,
    LoadedSet,
    load_set,
)
from gideon.evaluation.extraction_slice import SLICE_RUNNERS, SliceResult
from gideon.host import courts, site, stack
from gideon.host.report import StageResult, print_stage, refusal
from gideon.host.sysio import Host, PathLike, RealHost

_COMMAND: Final[str] = "eval run"
_FLAG_FIX: Final[str] = "Run gideon eval run --slice extraction; decision runs land in slice-2 ticket 16."
_SLICE_FIX: Final[str] = "Run gideon eval run --slice extraction."
_LOAD_FIX: Final[str] = "Correct every listed eval-set finding, then retry."
_GATE_FIX: Final[str] = "Review the miss and false hit lines in the extraction report, then retry."
_RECORD_ROOT_FIX: Final[str] = (
    "Run sudo python3 -m gideon eval run --slice extraction as root with the stack up, then retry."
)


def _print_findings(result: EvalSetLoadResult) -> None:
    for finding in result.findings:
        print(finding.text(), file=sys.stderr)


def _load(
    set_root: Path,
    *,
    court_path: Path,
    host: Host | None,
) -> EvalSetLoadResult:
    court_result = courts.load_court_map(court_path, host=host)
    if not court_result.ok or court_result.court_map is None:
        details = "; ".join(error.problem for error in court_result.errors)
        return EvalSetLoadResult(
            findings=(
                Finding(
                    court_path.as_posix(),
                    None,
                    0,
                    details or "court map could not be loaded",
                    "Restore courts.yaml from the release checkout, then retry.",
                ),
            )
        )
    return load_set(set_root, court_result.court_map.courts)


def _run_slice(loaded: LoadedSet, slice_name: str) -> SliceResult | None:
    runner = SLICE_RUNNERS.get(slice_name)
    if runner is None:
        return None
    return runner(loaded, slice_name)


def _git_fix(checkout: str) -> str:
    return (
        f"Run git -c safe.directory={checkout} -C {checkout} rev-parse HEAD and "
        f"git -c safe.directory={checkout} -C {checkout} status --porcelain as the checkout owner, then retry."
    )


def _provenance(
    io: Host, checkout: Path
) -> tuple[tuple[str | None, bool | None] | None, str | None]:
    """Read commit and dirty state, or return a record-stage refusal."""

    checkout_text = str(checkout)
    try:
        if not io.exists(checkout / ".git"):
            return (None, None), None
        commit = io.run(
            [
                "git",
                "-c",
                f"safe.directory={checkout_text}",
                "-C",
                checkout_text,
                "rev-parse",
                "HEAD",
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return None, _git_fix(checkout_text)
    if commit.returncode != 0 or not commit.stdout.strip():
        return None, _git_fix(checkout_text)

    try:
        status = io.run(
            [
                "git",
                "-c",
                f"safe.directory={checkout_text}",
                "-C",
                checkout_text,
                "status",
                "--porcelain",
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return None, _git_fix(checkout_text)
    if status.returncode != 0:
        return None, _git_fix(checkout_text)
    return (commit.stdout.strip(), bool(status.stdout.splitlines())), None


def _record(
    loaded: LoadedSet,
    slice_name: str,
    slice_result: SliceResult,
    *,
    started: datetime,
    finished: datetime,
    checkout: Path,
    host: Host,
    rendered_dir: PathLike,
    site_path: PathLike,
    supplied_set: bool,
    run_id: str,
) -> bool:
    if supplied_set:
        print_stage(
            StageResult(
                "record",
                True,
                "skipped — a set outside the release is never recorded",
                "",
            )
        )
        return True

    probe_problem = record.probe(host, rendered_dir)
    if probe_problem is not None:
        print_stage(
            StageResult(
                "record",
                True,
                f"skipped — no database reachable; rows were not written ({probe_problem})",
                _RECORD_ROOT_FIX,
            )
        )
        return True

    site_result = site.load_site(Path(site_path), host=host)
    if not site_result.ok or site_result.config is None:
        detail = "; ".join(error.problem for error in site_result.errors)
        fix = site_result.errors[0].fix if site_result.errors else "Create a valid site file, then retry."
        print_stage(StageResult("record", False, f"site file could not be loaded: {detail}", fix))
        return False

    provenance, provenance_fix = _provenance(host, checkout)
    if provenance is None:
        print_stage(
            StageResult(
                "record",
                False,
                "git provenance could not be read; rows were not written",
                provenance_fix or _git_fix(str(checkout)),
            )
        )
        return False
    git_sha, git_dirty = provenance
    run = record.RunRow(
        run_id=run_id,
        started_at=started,
        finished_at=finished,
        product_version=gideon.__version__,
        corpus_lockfile=None,
        eval_set_version=loaded.version,
        hardware_profile=site_result.config.hardware_profile,
        stack="production",
        generation_id=None,
        kind="manual",
        slice=slice_name,
        overrides={},
        repeats=1,
        git_sha=git_sha,
        git_dirty=git_dirty,
        set_digest=loaded.digest,
        verdict="pass" if slice_result.score.verdict else "fail",
    )
    results = tuple(
        record.ResultRow(
            run_id=run_id,
            run_started_at=started,
            case_id=result.case_id,
            repeat=1,
            verdict=result.verdict,
            metrics=result.metrics,
            judge=None,
            provenance_ref=None,
            latency_ms=result.latency_ms,
        )
        for result in slice_result.results
    )
    write_problem = record.write_rows(host, rendered_dir, run, results)
    if write_problem is not None:
        print_stage(
            StageResult(
                "record",
                False,
                f"run {run_id} and {len(results)} result rows were not written ({write_problem})",
                stack.logs_fix(rendered_dir, record.POSTGRES_SERVICE),
            )
        )
        return False
    print_stage(
        StageResult(
            "record",
            True,
            f"run {run_id} recorded (1 run row, {len(results)} result rows)",
            "",
        )
    )
    return True


def _run_body(
    args: argparse.Namespace,
    *,
    set_root: Path,
    court_path: Path,
    host: Host,
    checkout: Path,
    rendered_dir: PathLike,
    site_path: PathLike,
    started: datetime,
    run_id: str,
    supplied_set: bool,
    finished_clock: Callable[[], datetime],
) -> int:
    if getattr(args, "decision", False) or getattr(args, "force", False):
        print(refusal(_COMMAND, "decision and force flags are not implemented", _FLAG_FIX), file=sys.stderr)
        return 1

    slice_name = getattr(args, "slice", None)
    if not isinstance(slice_name, str) or not slice_name:
        print(refusal(_COMMAND, "no slice was selected", _SLICE_FIX), file=sys.stderr)
        return 1

    loaded_result = _load(set_root, court_path=court_path, host=host)
    if loaded_result.findings:
        _print_findings(loaded_result)
        print_stage(StageResult("load", False, "eval set refused", _LOAD_FIX))
        return 1
    loaded = loaded_result.loaded
    if loaded is None:
        print_stage(StageResult("load", False, "eval set was not loaded", _LOAD_FIX))
        return 1
    if slice_name not in loaded.slices:
        available = ", ".join(sorted(loaded.slices)) or "none"
        print_stage(
            StageResult(
                "load",
                False,
                f"unknown slice {slice_name!r}; available: {available}",
                _SLICE_FIX,
            )
        )
        return 1

    selected = loaded.slices[slice_name]
    print_stage(
        StageResult(
            "load",
            True,
            f"{loaded.version}: {len(loaded.cases_by_id)} cases, {len(selected)} in {slice_name}, digest {loaded.digest}",
            "",
        )
    )

    slice_result = _run_slice(loaded, slice_name)
    if slice_result is None:
        print_stage(
            StageResult(
                "run",
                False,
                f"no runner serves slice {slice_name!r}",
                "Implement the runner named by the slice, then retry.",
            )
        )
        return 1
    print_stage(
        StageResult(
            "run",
            True,
            f"{len(slice_result.results)} active cases evaluated",
            "",
        )
    )
    print(slice_result.report, end="")

    record_ok = _record(
        loaded,
        slice_name,
        slice_result,
        started=started,
        finished=finished_clock(),
        checkout=checkout,
        host=host,
        rendered_dir=rendered_dir,
        site_path=site_path,
        supplied_set=supplied_set,
        run_id=run_id,
    )
    if slice_result.score.verdict:
        print_stage(StageResult("gate", True, "extraction bounds passed", ""))
        return 0 if record_ok else 1
    print_stage(StageResult("gate", False, "extraction bounds failed", _GATE_FIX))
    return 1


def run_eval(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    checkout_root: PathLike | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered",
    site_path: PathLike = "/etc/gideon/site.yaml",
    clock: Callable[[], datetime] | None = None,
    run_id_factory: Callable[[], str] | None = None,
    court_path: PathLike | None = None,
) -> int:
    """Run ``eval run``'s ordered stages and return the exit code."""

    checkout = Path(__file__).parents[2] if checkout_root is None else Path(checkout_root)
    io = RealHost() if host is None else host
    now = (lambda: datetime.now(UTC)) if clock is None else clock
    run_id = str(uuid4()) if run_id_factory is None else run_id_factory()
    supplied_root = getattr(args, "set", None)
    set_root = checkout / SET_ROOT if supplied_root is None else Path(supplied_root)
    selected_court_path = courts.default_courts_path() if court_path is None else Path(court_path)
    return _run_body(
        args,
        set_root=set_root,
        court_path=selected_court_path,
        host=io,
        checkout=checkout,
        rendered_dir=rendered_dir,
        site_path=site_path,
        started=now(),
        run_id=run_id,
        supplied_set=supplied_root is not None,
        finished_clock=now,
    )
