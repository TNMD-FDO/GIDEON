"""Registry of evaluation slice runners."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from gideon.evaluation import extraction_slice, judge_slice, judgments_slice
from gideon.evaluation.evalset import LoadedSet
from gideon.evaluation.results import RunContext, SliceResult


@dataclass(frozen=True, slots=True)
class SliceSpec:
    """The runner, settings, and gate text for one named evaluation slice.

    ``compares_reference`` is whether ``eval run`` compares the slice with its
    committed reference and ``eval reference`` writes one. ``judge-triples``
    keeps none: a triple passes when its grading came back on-schema, which the
    slice's own gate already refuses on, so a regression list would say it twice.
    """

    runner: Callable[[LoadedSet, str, RunContext], SliceResult]
    reaches_engine: bool
    takes_ranked: bool
    repeats: int
    judge_prompt: str | None
    compares_reference: bool
    gate_pass: str
    gate_fail: str
    gate_fix: str


SLICE_RUNNERS: Final[Mapping[str, SliceSpec]] = {
    "extraction": SliceSpec(
        runner=extraction_slice.run_extraction,
        reaches_engine=False,
        takes_ranked=False,
        repeats=1,
        judge_prompt=None,
        compares_reference=True,
        gate_pass="extraction bounds passed",
        gate_fail="extraction bounds failed",
        gate_fix="Review the miss and false hit lines in the extraction report, then retry.",
    ),
    "judge-triples": SliceSpec(
        runner=judge_slice.run_judge_triples,
        reaches_engine=True,
        takes_ranked=False,
        repeats=2,
        judge_prompt="synthesis@1",
        compares_reference=False,
        gate_pass="all judge gradings returned on-schema verdicts",
        gate_fail="one or more judge gradings failed to return an on-schema verdict",
        gate_fix="Review the failed judge gradings and engine logs, then retry.",
    ),
    "judgments": SliceSpec(
        runner=judgments_slice.run_judgments,
        reaches_engine=False,
        takes_ranked=True,
        repeats=1,
        judge_prompt=None,
        compares_reference=False,
        gate_pass="every judged query was scored, the metrics reported and never gated (§18.3)",
        gate_fail="one or more judged queries have no ranked list",
        gate_fix="add a ranked list for each query id the report names, then retry.",
    ),
}
