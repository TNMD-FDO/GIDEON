"""Registry of evaluation slice runners."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final

from gideon.evaluation import (
    extraction_slice,
    general_smoke_slice,
    guardrails_slice,
    judge_slice,
    judgments_slice,
    smoke_slice,
)
from gideon.evaluation.decision import DecisionMetric
from gideon.evaluation.evalset import TIER_2_CATEGORY, LoadedSet
from gideon.evaluation.results import RunContext, SliceResult


class CallSurface(Enum):
    """A surface an evaluation slice reaches, and whether it is on the box."""

    ENGINE = ("engine", True)
    TURNS = ("turns", True)
    RANKED_FILE = ("ranked-file", False)

    def __init__(self, _name: str, on_box: bool) -> None:
        self.on_box = on_box


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
    a figure it reports and never gates. The turns surface makes ``eval
    run``'s ``preconditions`` resolve turn access and probe the door for a
    runner that uses the harness's drivers. ``general-smoke`` runs two repeats,
    so every run is the repeat that shows determinism rather than asserting it,
    and the nightly run takes the registry's count until it is ruled otherwise.
    It names turns though it drives no door turn: the reads the surface triggers
    are the password and client factory its runner needs, and the door probe
    proves General's service — which every frontend turn passes through —
    answers before the turns are spent.
    ``categories`` are the loader's shape-registry keys this runner runs;
    a composite names none because it runs no case itself. ``turn_calls``
    counts the turns this runner drives over a slice's counted cases when a
    composite routes to it, excluding the judge's readings.
    ``engine_calls`` estimates the engine requests one counted run makes; None
    leaves its size bound to the quiet window. ``smoke``'s is the sum of its
    parts' ``turn_calls``; ``guardrails`` run alone keeps None, since the
    judge's readings it then asks for depend on the answers.
    A slice with a decision metric runs one repeat per call so the command can
    repeat it; its ``repeats`` value is one.
    """

    runner: Callable[[LoadedSet, str, RunContext], SliceResult]
    surfaces: frozenset[CallSurface]
    repeats: int
    judge_prompt: str | None
    compares_reference: bool
    engine_calls: Callable[[LoadedSet, str], int] | None
    gate_pass: str
    gate_fail: str
    gate_fix: str
    categories: frozenset[tuple[str, str]]
    turn_calls: Callable[[LoadedSet, str], int] | None
    decision: DecisionMetric | None = None

    def __post_init__(self) -> None:
        if CallSurface.TURNS in self.surfaces and CallSurface.ENGINE not in self.surfaces:
            raise ValueError(
                "a slice naming the turns surface must name the engine surface: "
                "every turn passes through the engine, its window, and its lock"
            )

    @property
    def reaches_box(self) -> bool:
        return any(surface.on_box for surface in self.surfaces)


_EXTRACTION: Final[SliceSpec] = SliceSpec(
    runner=extraction_slice.run_extraction,
    surfaces=frozenset(),
    repeats=1,
    judge_prompt=None,
    compares_reference=True,
    engine_calls=None,
    gate_pass="extraction bounds passed",
    gate_fail="extraction bounds failed",
    gate_fix="Review the miss and false hit lines in the extraction report, then retry.",
    categories=frozenset({("build-gates", "extraction")}),
    turn_calls=None,
)
_JUDGE_TRIPLES: Final[SliceSpec] = SliceSpec(
    runner=judge_slice.run_judge_triples,
    surfaces=frozenset({CallSurface.ENGINE}),
    repeats=2,
    judge_prompt="synthesis@1",
    compares_reference=False,
    engine_calls=None,
    gate_pass="all judge gradings returned on-schema verdicts",
    gate_fail="one or more judge gradings failed to return an on-schema verdict",
    gate_fix="Review the failed judge gradings and engine logs, then retry.",
    categories=frozenset({("judge", "triples")}),
    turn_calls=None,
)
_JUDGMENTS: Final[SliceSpec] = SliceSpec(
    runner=judgments_slice.run_judgments,
    surfaces=frozenset({CallSurface.RANKED_FILE}),
    repeats=1,
    judge_prompt=None,
    compares_reference=False,
    engine_calls=None,
    gate_pass="every judged query was scored, the metrics reported and never gated",
    gate_fail="one or more judged queries have no ranked list",
    gate_fix="add a ranked list for each query id the report names, then retry.",
    categories=frozenset({("judgments", "judgments")}),
    turn_calls=None,
)
_GUARDRAILS: Final[SliceSpec] = SliceSpec(
    runner=guardrails_slice.run_guardrails,
    surfaces=frozenset({CallSurface.ENGINE, CallSurface.TURNS}),
    repeats=1,
    judge_prompt="false-refusal@1",
    compares_reference=True,
    engine_calls=None,
    gate_pass="every positive blocked, over-trips within the ceiling, no leak, the frontend sample agreeing",
    gate_fail="a family's gate failed",
    gate_fix="Review the per-family report lines and the ids they list, then retry.",
    categories=frozenset(
        {
            ("guardrails", "deadline-trap"),
            ("guardrails", "guidelines-range"),
            ("guardrails", "sentence-credit"),
            ("guardrails", TIER_2_CATEGORY),
        }
    ),
    turn_calls=guardrails_slice.turn_calls,
    decision=guardrails_slice.FALSE_REFUSAL,
)
_GENERAL_SMOKE: Final[SliceSpec] = SliceSpec(
    runner=general_smoke_slice.run_general_smoke,
    surfaces=frozenset({CallSurface.ENGINE, CallSurface.TURNS}),
    repeats=2,
    judge_prompt=None,
    compares_reference=True,
    engine_calls=None,
    gate_pass="every case met its expectation and checks on every repeat, the stream was clean, every chat was deleted",
    gate_fail="a case failed its expectation, a check, its stream, or its cleanup",
    gate_fix="Review the per-case report lines and the checks they name, then retry.",
    categories=frozenset({("general", "smoke")}),
    turn_calls=None,
)

SMOKE_PARTS: Final[Mapping[str, SliceSpec]] = MappingProxyType(
    {"guardrails": _GUARDRAILS, "extraction": _EXTRACTION}
)
_SMOKE: Final[smoke_slice.Composite] = smoke_slice.Composite(SMOKE_PARTS)

SLICE_RUNNERS: Final[Mapping[str, SliceSpec]] = {
    "extraction": _EXTRACTION,
    "judge-triples": _JUDGE_TRIPLES,
    "judgments": _JUDGMENTS,
    "guardrails": _GUARDRAILS,
    "general-smoke": _GENERAL_SMOKE,
    "smoke": SliceSpec(
        runner=_SMOKE.run,
        surfaces=frozenset().union(*(spec.surfaces for spec in SMOKE_PARTS.values())),
        repeats=1,
        judge_prompt=None,
        compares_reference=True,
        engine_calls=_SMOKE.engine_calls,
        gate_pass=(
            "every positive blocked, no leak, the frontend sample agreeing; "
            "the controls' replacement and the extraction bounds reported"
        ),
        gate_fail="a zero-tolerance case failed",
        gate_fix=(
            "Review the smoke report's unblocked, leak, error, and disagreeing ids, then retry."
        ),
        categories=frozenset(),
        turn_calls=None,
    ),
}
