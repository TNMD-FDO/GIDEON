"""Record one content-free tally of the improvement report in the audit log."""

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from gideon.evaluation.command import git_argv
from gideon.host import audit
from gideon.host.sysio import Host, PathLike
from gideon.improvement.sections import SectionReport
from gideon.improvement.watch import TRIGGERS_SECTION

if TYPE_CHECKING:
    from gideon.improvement.proposals import WalkResult

TALLY_KIND: Final[str] = "proposals_tally"
DETAIL_KEYS: Final[tuple[str, ...]] = (
    "fired",
    "sections",
    "skipped",
    "refused",
    "triggers",
    "git_dirty",
)


def git_dirty(io: Host, checkout: Path) -> bool | None:
    """Read the checkout's dirty state, or leave provenance unknown."""

    try:
        if not io.exists(checkout / ".git"):
            return None
        status = io.run(git_argv(checkout, "status", "--porcelain"))
    except OSError:
        return None
    if status.returncode != 0:
        return None
    return bool(status.stdout.splitlines())


def tally_detail(walk: "WalkResult", dirty: bool | None) -> Mapping[str, object]:
    """Build the six closed detail fields from the report's own outcomes."""

    trigger_rows = [
        {"id": row.name, "state": row.state, "detail": row.detail}
        for outcome in walk.outcomes
        if outcome.section.name == TRIGGERS_SECTION.name and isinstance(outcome.result, SectionReport)
        for row in outcome.result.rows
    ]
    return {
        "fired": walk.fired,
        "sections": len(walk.outcomes),
        "skipped": walk.skipped,
        "refused": walk.refused,
        "triggers": trigger_rows,
        "git_dirty": dirty,
    }


def write_tally(io: Host, rendered_dir: PathLike, checkout: Path, walk: "WalkResult") -> str | None:
    """Append one audit row and return the writer's problem, if any."""

    row = audit.AuditRow(
        run_id=str(uuid4()),
        kind=TALLY_KIND,
        actor_user_id=None,
        user_id=None,
        chat_id=None,
        kb_ids=(),
        detail=tally_detail(walk, git_dirty(io, checkout)),
    )
    return audit.write_rows(io, rendered_dir, (row,))
