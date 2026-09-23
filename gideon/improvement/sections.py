"""Define improvement report sections and their read-only host context.

Sections have ``product`` scope, printed only on a build box, or ``office``
scope, printed everywhere. Rows contain only ids, section numbers, register
tags, figure names, and numbers. Sections never read the build-box marker, and
this module's query seam is the report's only path to Postgres.
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from gideon.evaluation.record import (
    EVAL_DATABASE,
    METRICS_ROLE,
    POSTGRES_SERVICE,
)
from gideon.host import stack
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike
from gideon.improvement.triggers import TriggerRegistry

type Scope = Literal["product", "office"]
type RowState = Literal[
    "fired", "not fired", "not yet measurable", "skipped", "refuse"
]


@dataclass(frozen=True, slots=True)
class Row:
    """One content-free section row."""

    name: str
    state: RowState
    detail: str


@dataclass(frozen=True, slots=True)
class SectionReport:
    """A section header detail and its ordered rows."""

    detail: str
    rows: tuple[Row, ...]


@dataclass(frozen=True, slots=True)
class Context:
    """Read-only inputs shared with every improvement section."""

    host: Host
    checkout_root: Path
    rendered_dir: Path
    registry: TriggerRegistry
    build_box: bool
    query: Callable[[str], tuple[str, ...] | Problem]


class Section(Protocol):
    """One report section with a scope and a context-based renderer."""

    @property
    def name(self) -> str: ...

    @property
    def scope(self) -> Scope: ...

    def render(self, context: Context) -> SectionReport | Problem: ...


READ_FIX: Final[str] = (
    "Run sudo python3 -m gideon proposals as root with the stack up, then retry."
)


def read_rows(
    host: Host, rendered_dir: PathLike, sql: str
) -> tuple[str, ...] | Problem:
    """Run a metrics-role read query and return its non-empty output rows."""

    argv = stack.exec_argv(
        rendered_dir,
        POSTGRES_SERVICE,
        "psql",
        "-U",
        METRICS_ROLE,
        "-d",
        EVAL_DATABASE,
        "-v",
        "ON_ERROR_STOP=1",
        "-tA",
        "-f",
        "-",
    )
    try:
        result = host.run(argv, input=sql)
    except (OSError, subprocess.SubprocessError):
        return Problem("metrics reader command could not run", READ_FIX)
    if result.returncode != 0:
        return Problem(
            f"metrics reader failed with exit code {result.returncode}", READ_FIX
        )
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
