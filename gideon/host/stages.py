"""Helpers the ordered host commands (backup, push, restore, drill) share."""

import re
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from gideon.host import site, stack
from gideon.host.report import StageResult, command_detail
from gideon.host.sysio import Host, PathLike

# A SQL identifier the counts accept: unquoted, lower-case, never interpolated
# from anything but pg_stat_user_tables.
IDENTIFIER: Final = re.compile(r"^[a-z_][a-z0-9_]*$")


def run_stage(
    io: Host,
    name: str,
    argv: Sequence[str],
    detail: str,
    fix: str,
    *,
    input: str | None = None,
    timeout: float | None = None,
) -> StageResult:
    """Run one command as a stage: its exit status decides the row."""

    try:
        result = io.run(argv, input=input, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult(name, False, f"{detail}: {exc}", fix)
    if result.returncode != 0:
        return StageResult(name, False, f"{detail}: {command_detail(result)}", fix)
    return StageResult(name, True, detail, "")


def aware_now(value: datetime | None) -> datetime | None:
    """The injected clock as UTC, the real clock when absent, None when naive."""

    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def site_problem(result: site.SiteLoadResult) -> str:
    return "; ".join(error.problem for error in result.errors)


def psql_argv(project_dir: PathLike, database: str) -> list[str]:
    """One unaligned, tuples-only psql over the project's Postgres, SQL on stdin."""

    return stack.exec_argv(
        project_dir, "postgres", "psql", "-U", "postgres", "-d", database, "-tA", "-f", "-"
    )


def table_parts(name: str) -> tuple[str, str] | None:
    """Split ``schema.table`` when both parts satisfy the identifier grammar."""

    parts = name.split(".")
    if len(parts) != 2 or any(IDENTIFIER.fullmatch(part) is None for part in parts):
        return None
    return parts[0], parts[1]
