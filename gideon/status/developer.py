"""Read and format the build box's developer status facts and proposals."""

import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from gideon.evaluation import record
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike
from gideon.improvement import sections
from gideon.status import fired, glance

_UP_FIX: Final[str] = "Run sudo python3 -m tools.cistack up, then retry."
_STATUS_FIX: Final[str] = "Run sudo python3 -m tools.cistack status, then retry."
_SMOKE_FIX: Final[str] = "Run sudo python3 -m tools.cistack smoke, then retry."
_NONE_YET: Final[str] = "none yet"
_NOT_CONVERGED: Final[str] = "down, not converged"
_NONE_FIRED: Final[str] = "proposals: none fired"
_EVAL_RUN: Final[str] = "{slice} {kind} on {stack}: {verdict}{partial}, {age} ago"


def sibling_fact(host: Host, ci_root: PathLike) -> glance.Fact:
    """Report how many of the sibling's declared services are running."""

    name = "sibling stack"
    try:
        converged = host.exists(Path(ci_root) / "compose.yaml")
    except (OSError, subprocess.SubprocessError):
        return glance.failure(name, "sibling Compose file is unavailable.", _STATUS_FIX)
    if not converged:
        return glance.Fact(name, _NOT_CONVERGED, _UP_FIX)

    counts = glance.service_counts(host, ci_root)
    if isinstance(counts, Problem):
        return glance.failure(name, counts.problem, _STATUS_FIX)
    declared, count, missing = counts
    if not missing:
        return glance.Fact(name, f"up, {count} of {len(declared)} services", "")
    if count == 0:
        return glance.Fact(name, f"down, 0 of {len(declared)} services", _UP_FIX)
    return glance.Fact(
        name,
        f"{count} of {len(declared)} services up; not running: {', '.join(missing)}",
        _STATUS_FIX,
    )


def eval_run_fact(host: Host, rendered_dir: PathLike, now: datetime) -> glance.Fact:
    """Report the newest recorded evaluation run and its age."""

    name = "eval run"
    newest, problem = record.read_newest_run(host, rendered_dir)
    if problem is not None:
        return glance.failure(name, problem.problem, problem.fix)
    if newest is None:
        return glance.Fact(name, _NONE_YET, _SMOKE_FIX)
    detail = _EVAL_RUN.format(
        slice=newest.slice if newest.slice is not None else "no slice",
        kind=newest.kind,
        stack=newest.stack,
        verdict=newest.verdict,
        partial=" (partial)" if newest.partial else "",
        age=glance.age_text(now, newest.finished_at),
    )
    return glance.Fact(name, detail, "")


def proposal_lines(
    context: sections.Context, registered: Sequence[sections.Section]
) -> tuple[str, ...]:
    """Render fired product rows and report any section read failures."""

    return fired.lines(
        context,
        registered,
        scope="product",
        failure="{section}: could not read — {problem} Fix: {fix}",
        row="{section}: {name} — {detail}",
        empty=_NONE_FIRED,
    )
