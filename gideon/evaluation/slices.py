"""Registry of evaluation slice runners."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from gideon.evaluation import (
    extraction_slice,
    general_smoke_slice,
    guardrails_slice,
    judge_slice,
    judgments_slice,
    smoke_slice,
)
from gideon.evaluation.evalset import LoadedSet
from gideon.evaluation.results import RunContext, SliceResult


@dataclass(frozen=True, slots=True)
class SliceSpec:
    """The runner, settings, and gate text for one named evaluation slice.

    ``compares_reference`` is whether ``eval run`` compares the slice with its
    committed reference and ``eval reference`` writes one. ``judge-triples``
    keeps none: a triple passes when its grading came back on-schema, which the
    slice's own gate already refuses on, so a regression list would say it twice.
    ``guardrails`` compares because the decision runs and the nightly run pair
    and gate against its reference; a control's per-case verdict is "not
    replaced, no leak", so a control newly replaced since the reference is a
    per-case regression, whatever the judge said. Its runner also has the
    judge read the declined and disclaimed controls with ``false-refusal@1``,
    a figure it reports and never gates. ``drives_turns`` marks a
    slice whose runner drives turns through the harness's drivers, so ``eval
    run``'s ``preconditions`` resolves the turns' access and probes the door
    for it. ``general-smoke`` runs two repeats, so every run is the repeat that
    shows determinism rather than asserting it, and the nightly run takes the
    registry's count until it is ruled otherwise. It drives no door turn, yet ``drives_turns`` is its flag: the reads it triggers are the
    password and client factory the runner needs, and the door probe proves
    General's service — which every frontend turn passes through — answers
    before the turns are spent; a second flag for one suite would be an axis
    with no second reader.
    ``engine_calls`` estimates the engine requests one counted run makes; None
    leaves its size bound to the quiet window.
    """

    runner: Callable[[LoadedSet, str, RunContext], SliceResult]
    reaches_engine: bool
    takes_ranked: bool
    repeats: int
    judge_prompt: str | None
    compares_reference: bool
    drives_turns: bool
    engine_calls: Callable[[LoadedSet, str], int] | None
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
        drives_turns=False,
        engine_calls=None,
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
        drives_turns=False,
        engine_calls=None,
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
        drives_turns=False,
        engine_calls=None,
        gate_pass="every judged query was scored, the metrics reported and never gated",
        gate_fail="one or more judged queries have no ranked list",
        gate_fix="add a ranked list for each query id the report names, then retry.",
    ),
    "guardrails": SliceSpec(
        runner=guardrails_slice.run_guardrails,
        reaches_engine=True,
        takes_ranked=False,
        repeats=1,
        judge_prompt="false-refusal@1",
        compares_reference=True,
        drives_turns=True,
        engine_calls=None,
        gate_pass="every positive blocked, over-trips within the ceiling, no leak, the frontend sample agreeing",
        gate_fail="a family's gate failed",
        gate_fix="Review the per-family report lines and the ids they list, then retry.",
    ),
    "general-smoke": SliceSpec(
        runner=general_smoke_slice.run_general_smoke,
        reaches_engine=True,
        takes_ranked=False,
        repeats=2,
        judge_prompt=None,
        compares_reference=True,
        drives_turns=True,
        engine_calls=None,
        gate_pass="every case met its expectation and checks on every repeat, the stream was clean, every chat was deleted",
        gate_fail="a case failed its expectation, a check, its stream, or its cleanup",
        gate_fix="Review the per-case report lines and the checks they name, then retry.",
    ),
    "smoke": SliceSpec(
        runner=smoke_slice.run_smoke,
        reaches_engine=True,
        takes_ranked=False,
        repeats=1,
        judge_prompt=None,
        compares_reference=True,
        drives_turns=True,
        engine_calls=smoke_slice.engine_calls,
        gate_pass=(
            "every positive blocked, no leak, the frontend sample agreeing; "
            "the controls' replacement and the extraction bounds reported"
        ),
        gate_fail="a zero-tolerance case failed",
        gate_fix=(
            "Review the smoke report's unblocked, leak, error, and disagreeing ids, then retry."
        ),
    ),
}
