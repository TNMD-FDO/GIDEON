"""Share frontend sessions and turn-row readings across evaluation suites."""

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gideon import guardrail
from gideon.evaluation.turns import access, cases, run
from gideon.host.render.owui import EVAL_IDENTITY

PROBLEMS: Final[frozenset[str]] = frozenset({"unverified", "cleanup-failed", "turn-cut"})
"""Closed codes a managed row's reading can return, before a suite's own checks."""
CLEANUP: Final[frozenset[str]] = frozenset({"unverified", "cleanup-failed"})
"""The codes of a chat left behind or never identified, which owe the cleanup fix."""


@dataclass(frozen=True, slots=True)
class Reading:
    """A managed row's cleanup problem and reported pattern."""

    problem: str | None
    pattern_id: str | None


def read(row: run.TurnRow) -> Reading:
    """Read a managed turn's cleanup problem before its suite-specific checks."""

    if row.chat_id is None:
        problem = "unverified"
    elif not row.deleted:
        problem = "cleanup-failed"
    elif row.cut:
        problem = "turn-cut"
    else:
        problem = None
    return Reading(problem, row.reported_pattern)


def cleanup_fix(problems: Iterable[str | None]) -> str | None:
    """Return the cleanup instruction when any reported problem requires it."""

    if any(problem in CLEANUP for problem in problems):
        return run.unverified_fix(EVAL_IDENTITY.username)
    return None


class ManagedTurns:
    """One signed-in API frontend session for a suite's managed turns."""

    def __init__(
        self,
        turn_access: access.TurnAccess,
        *,
        cases: Path,
        repeat: int,
        stream: bool,
    ) -> None:
        self._driver = run.ApiTurnDriver(turn_access.client_factory, turn_access.password)
        self._spec = run.RunSpec(
            cases=cases,
            repeat=repeat,
            stream=stream,
            out=None,
            force=False,
            dry_run=False,
            sentinel=turn_access.sentinel,
        )

    def signin(self) -> str:
        """Sign in and return the frontend's opening detail."""

        return self._driver.signin()

    @property
    def client(self) -> run.Client:
        """The signed-in frontend client."""

        return self._driver.client

    def turn(self, case: cases.Case, *, row_name: str) -> run.TurnRow:
        """Make one frontend turn with the harness's fixed suite arguments."""

        return run.frontend_turn(
            self._spec,
            client=self.client,
            driver=self._driver,
            guardrail=guardrail,
            case=case,
            session_number=1,
            row_name=row_name,
            now=lambda: datetime.now(UTC),
            monotonic=time.monotonic,
        )
