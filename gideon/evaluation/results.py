"""Shared result types for evaluation runners."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gideon.evaluation.rankmetrics import Coordinates
from gideon.host.sysio import Host, PathLike

if TYPE_CHECKING:
    from gideon.evaluation.turns.access import TurnAccess

type JSONValue = None | bool | int | float | str | list[JSONValue] | Mapping[str, JSONValue]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One content-free case result, with judge-derived values kept in ``judge``.

    Everything except ``judge`` is content-free, and nothing derived from a
    judge's score may sit outside ``judge``.
    """

    case_id: str
    repeat: int
    verdict: str
    metrics: Mapping[str, JSONValue]
    judge: Mapping[str, JSONValue] | None = None
    latency_ms: float | None = None

    def __repr__(self) -> str:
        """Keep the judge's reason out of diagnostic representations.

        ``judge`` is the one field that can carry model-written text, so the
        generated repr would otherwise print the reason that ``judge.Verdict``
        takes care to redact.
        """

        judge = None if self.judge is None else "<redacted>"
        return (
            "CaseResult("
            f"case_id={self.case_id!r}, repeat={self.repeat!r}, "
            f"verdict={self.verdict!r}, metrics={self.metrics!r}, "
            f"judge={judge}, latency_ms={self.latency_ms!r})"
        )


@dataclass(frozen=True, slots=True)
class SliceResult:
    """The code-computed gate verdict, report, and per-case results."""

    verdict: bool
    report: str
    results: tuple[CaseResult, ...]


def _no_checkpoint() -> None:
    """Let a run with no deadline pass every checkpoint."""


@dataclass(frozen=True, slots=True)
class RunContext:
    """The host seams and runner settings for one evaluation slice.

    ``turns_dir`` is the rendered directory of the stack whose turns run.
    ``production_dir`` is the rendered directory of the project whose engine the judge calls.
    ``checkout`` is the tree the run takes its cases, reference, and provenance
    from; a runner reads release content such as seeds beneath it, never beside
    its own module. It is none when no command supplied a checkout.
    ``turns`` is present for a slice that drives turns through the harness's
    service or managed frontend drivers.
    ``checkpoint`` is called by a runner between cases and before a case's
    further turns, and a runner never catches what it raises: past the run's
    deadline it raises ``window.WindowOverrun``, so no turn starts after the
    window's end. The command also calls it around every runner call, which
    bounds a runner that never calls it; the default is a no-op.
    """

    host: Host
    turns_dir: PathLike
    production_dir: PathLike
    served_model_name: str | None
    judge_prompt_id: str | None
    repeats: int
    progress: Callable[[str], None]
    ranked: Mapping[str, tuple[Coordinates, ...]] | None = None
    turns: TurnAccess | None = None
    checkpoint: Callable[[], None] = _no_checkpoint
    checkout: PathLike | None = None
