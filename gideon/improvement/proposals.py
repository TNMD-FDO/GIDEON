"""Run the read-only report over registered improvement sections."""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from gideon.host import nogpu, owui
from gideon.host.report import Problem, one_line, refusal
from gideon.host.sysio import Host, PathLike, RealHost
from gideon.improvement import owuifeedback, ratings, triggers, trips
from gideon.improvement.sections import (
    Context,
    Row,
    RowState,
    Section,
    once,
    read_rows,
)
from gideon.improvement.watch import TRIGGERS_SECTION

SECTIONS: Final[tuple[Section, ...]] = (
    TRIGGERS_SECTION,
    ratings.FEEDBACK_SECTION,
    trips.TRIPS_SECTION,
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

    del args
    checkout = Path(__file__).parents[2] if checkout_root is None else Path(checkout_root)
    io = RealHost() if host is None else host
    rendered = Path(rendered_dir)
    registry_path = checkout / "config/triggers.yaml" if triggers_path is None else triggers_path
    loaded = triggers.load_trigger_registry(registry_path, host=io)
    if loaded.errors or loaded.registry is None:
        return _registry_refusal(loaded.errors)

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
    refused = False
    fired = 0
    skipped = 0
    for section in registered:
        if section.scope == "product" and not context.build_box:
            skipped += 1
            _print_section(section, f"skipped — {nogpu.NOT_BUILD_BOX_DETAIL}")
            continue

        report = section.render(context)
        if isinstance(report, Problem):
            refused = True
            row = Row(
                section.name,
                "refuse",
                f"{one_line(report.problem)} Fix: {one_line(report.fix)}",
            )
            _print_section(section, "refused", (row,))
            continue

        fired += sum(row.state == "fired" for row in report.rows)
        _print_section(section, report.detail, report.rows)

    print(
        CLOSING_LINE.format(
            fired=fired,
            sections=len(registered),
            skipped=skipped,
        )
    )
    return int(refused)
