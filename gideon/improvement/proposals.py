"""Run the improvement report over registered sections and optionally record its tally."""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.host import audit, nogpu, owui, report, stack
from gideon.host.report import Problem, one_line, refusal
from gideon.host.sysio import Host, PathLike, RealHost
from gideon.improvement import owuifeedback, pairs, ratings, triggers, trips, upstream
from gideon.improvement.sections import (
    Context,
    Row,
    RowState,
    Section,
    SectionReport,
    once,
    read_rows,
)
from gideon.improvement.watch import TRIGGERS_SECTION

SECTIONS: Final[tuple[Section, ...]] = (
    TRIGGERS_SECTION,
    ratings.FEEDBACK_SECTION,
    trips.TRIPS_SECTION,
    upstream.UPSTREAM_SECTION,
    pairs.CHALLENGER_SECTION,
)
ROW_STATES: Final[tuple[RowState, ...]] = (
    "fired",
    "not fired",
    "not yet measurable",
    "skipped",
    "refuse",
    "rated",
)
SECTION_HEADER: Final[str] = "section {name} ({scope}): {detail}"
CLOSING_LINE: Final[str] = "proposals: {fired} fired, {sections} sections, {skipped} skipped"
_REGISTRY_FIX: Final[str] = "Restore config/triggers.yaml from the release checkout, then retry."
RECORDED_LINE: Final[str] = "proposals: recorded {fired} fired"


def _root_fix() -> str:
    record_command = report.command("proposals --record", sudo=False)
    return f"Run {record_command} as root, for example with sudo."


@dataclass(frozen=True, slots=True)
class SectionOutcome:
    """One section's rendered report, refusal, or build-box skip."""

    section: Section
    result: SectionReport | Problem | None


@dataclass(frozen=True, slots=True)
class WalkResult:
    """The ordered section outcomes and the report's three totals."""

    outcomes: tuple[SectionOutcome, ...]
    fired: int
    skipped: int
    refused: int


def walk_sections(context: Context, registered: Sequence[Section]) -> WalkResult:
    """Evaluate each section once and retain the outcomes for print and tally."""

    outcomes: list[SectionOutcome] = []
    fired = skipped = refused = 0
    for section in registered:
        if section.scope == "product" and not context.build_box:
            skipped += 1
            outcomes.append(SectionOutcome(section, None))
            continue
        report = section.render(context)
        if isinstance(report, Problem):
            refused += 1
        else:
            fired += sum(row.state == "fired" for row in report.rows)
        outcomes.append(SectionOutcome(section, report))
    return WalkResult(tuple(outcomes), fired, skipped, refused)


def _print_walk(walk: WalkResult) -> None:
    for outcome in walk.outcomes:
        section, report = outcome.section, outcome.result
        if report is None:
            _print_section(section, f"skipped — {nogpu.NOT_BUILD_BOX_DETAIL}")
        elif isinstance(report, Problem):
            row = Row(
                section.name,
                "refuse",
                f"{one_line(report.problem)} Fix: {one_line(report.fix)}",
            )
            _print_section(section, "refused", (row,))
        else:
            _print_section(section, report.detail, report.rows)
    print(
        CLOSING_LINE.format(
            fired=walk.fired,
            sections=len(walk.outcomes),
            skipped=walk.skipped,
        )
    )


def _print_section(section: Section, detail: str, rows: Sequence[Row] = ()) -> None:
    print(
        SECTION_HEADER.format(
            name=section.name,
            scope=section.scope,
            detail=one_line(detail),
        )
    )
    for row in rows:
        print(f"  {one_line(row.name)}: {row.state} — {one_line(row.detail)}")


def _registry_refusal(errors: Sequence[triggers.TriggerError]) -> int:
    if errors:
        print(triggers.render_errors(errors), file=sys.stderr)
        fix = errors[0].fix
    else:
        fix = _REGISTRY_FIX
    print(refusal("proposals", "trigger registry could not be loaded", fix), file=sys.stderr)
    return 1


def run_proposals(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    checkout_root: PathLike | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered",
    triggers_path: PathLike | None = None,
    sections: Sequence[Section] | None = None,
    site_path: PathLike = "/etc/gideon/site.yaml",
    client_factory: Callable[..., owui.Client] | None = None,
) -> int:
    """Load the registry, walk sections, and return the report exit code."""

    checkout = Path(__file__).parents[2] if checkout_root is None else Path(checkout_root)
    io = RealHost() if host is None else host
    rendered = Path(rendered_dir)
    record = bool(getattr(args, "record", False))
    if record and io.geteuid() != 0:
        print(refusal("proposals", "root is required to record the tally.", _root_fix()), file=sys.stderr)
        return 1
    registry_path = checkout / "config/triggers.yaml" if triggers_path is None else triggers_path
    loaded = triggers.load_trigger_registry(registry_path, host=io)
    if loaded.errors or loaded.registry is None:
        return _registry_refusal(loaded.errors)
    if record:
        audit_problem = audit.probe(io, rendered)
        if audit_problem is not None:
            print(
                refusal(
                    "proposals",
                    f"audit writer is unavailable: {audit_problem}",
                    stack.logs_fix(rendered, "postgres"),
                ),
                file=sys.stderr,
            )
            return 1

    build_box = nogpu.is_build_box(io)
    context = Context(
        host=io,
        checkout_root=checkout,
        rendered_dir=rendered,
        registry=loaded.registry,
        build_box=build_box,
        query=lambda sql: read_rows(io, rendered, sql),
        feedback=once(owuifeedback.source(io, site_path, client_factory).read),
        now=time.time,
    )
    registered = SECTIONS if sections is None else tuple(sections)
    walk = walk_sections(context, registered)
    _print_walk(walk)
    if record:
        # Imported here so the bare report never loads the evaluation package.
        from gideon.improvement import tally

        problem = tally.write_tally(io, rendered, checkout, walk)
        if problem is not None:
            print(
                refusal(
                    "proposals",
                    f"the tally row was not written: {problem}",
                    stack.logs_fix(rendered, "postgres"),
                ),
                file=sys.stderr,
            )
            return 1
        print(RECORDED_LINE.format(fired=walk.fired))
    return int(walk.refused > 0)
