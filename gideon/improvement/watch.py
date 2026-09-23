"""Evaluate committed triggers and render the product trigger section."""

from dataclasses import dataclass
from typing import Final, Literal

from gideon.host.report import Problem
from gideon.improvement import triggers
from gideon.improvement.measures import READERS, Figures
from gideon.improvement.sections import Context, Row, Scope, SectionReport

type ClauseState = Literal["holds", "fails", "unmeasurable"]
type TriggerState = Literal["fired", "not fired", "not yet measurable"]


@dataclass(frozen=True, slots=True)
class ClauseVerdict:
    """The values measured for one clause and its computed state."""

    clause: triggers.Clause
    values: tuple[int | float, ...]
    measured: int
    state: ClauseState


@dataclass(frozen=True, slots=True)
class TriggerVerdict:
    """A trigger's ordered clause verdicts and overall state."""

    trigger: triggers.Trigger
    clauses: tuple[ClauseVerdict, ...]
    state: TriggerState


def evaluate_clause(
    clause: triggers.Clause, figures: Figures
) -> ClauseVerdict:
    """Evaluate a clause against its newest values, newest first."""

    supplied = figures.get(clause.figure, ())
    values = tuple(supplied[: clause.runs])
    if len(values) < clause.runs:
        state: ClauseState = "unmeasurable"
    else:
        compare = {
            "above": lambda value: value > clause.value,
            "at-least": lambda value: value >= clause.value,
            "below": lambda value: value < clause.value,
        }[clause.op]
        state = "holds" if all(compare(value) for value in values) else "fails"
    return ClauseVerdict(clause, values, len(values), state)


def evaluate_trigger(
    trigger: triggers.Trigger, figures: Figures
) -> TriggerVerdict:
    """Compute a trigger verdict from its condition and supplied figures."""

    condition = trigger.condition
    clause_verdicts = (
        ()
        if condition is None
        else tuple(evaluate_clause(clause, figures) for clause in condition.clauses)
    )
    if any(clause.state == "fails" for clause in clause_verdicts):
        state: TriggerState = "not fired"
    elif any(clause.state == "unmeasurable" for clause in clause_verdicts):
        state = "not yet measurable"
    else:
        state = "fired"
    return TriggerVerdict(trigger, clause_verdicts, state)


def render_row(verdict: TriggerVerdict) -> Row:
    """Render a content-free trigger row from its computed verdict."""

    trigger = verdict.trigger
    details: list[str] = []
    for item in verdict.clauses:
        clause = item.clause
        newest = str(item.values[0]) if item.values else "unmeasured"
        detail = f"{clause.figure} {newest} {clause.op} {clause.value}"
        if clause.runs > 1:
            detail += (
                f" on {clause.runs} runs "
                f"({item.measured} of {clause.runs} measured)"
            )
        details.append(detail)
    reopens = f"reopens {trigger.reopens}"
    if trigger.register is not None:
        reopens += f" ({trigger.register})"
    details.append(reopens)
    if trigger.baseline is not None:
        details.append(f"baseline {trigger.baseline}")
    return Row(trigger.id, verdict.state, "; ".join(details))


@dataclass(frozen=True, slots=True)
class TriggersSection:
    """Report the committed trigger registry's watching entries."""

    name: str = "triggers"
    scope: Scope = "product"

    def render(self, context: Context) -> SectionReport | Problem:
        """Read each required measure once and render all watching triggers."""

        figures_by_measure: dict[triggers.TriggerMeasure, Figures] = {}
        measures = dict.fromkeys(
            trigger.measure for trigger in context.registry.watching
        )
        for measure in measures:
            if measure == "unavailable":
                figures_by_measure[measure] = {}
                continue
            reader = READERS[measure]
            figures = reader.read(context)
            if isinstance(figures, Problem):
                return figures
            figures_by_measure[measure] = figures

        rows = tuple(
            render_row(
                evaluate_trigger(trigger, figures_by_measure[trigger.measure])
            )
            for trigger in context.registry.watching
        )
        counts = {
            state: sum(trigger.state == state for trigger in context.registry.triggers)
            for state in triggers.STATES
        }
        detail = (
            f"{counts['watching']} watching, {counts['acted']} acted, "
            f"{counts['retired']} retired"
        )
        return SectionReport(detail, rows)


TRIGGERS_SECTION: Final[TriggersSection] = TriggersSection()
