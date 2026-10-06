"""Collect fired improvement rows for the status report's two scopes."""

from collections.abc import Sequence

from gideon.host.report import Problem
from gideon.improvement import sections


def lines(
    context: sections.Context,
    registered: Sequence[sections.Section],
    *,
    scope: sections.Scope,
    failure: str,
    row: str,
    empty: str,
) -> tuple[str, ...]:
    """Walk sections in order and word failures and fired rows for one block."""

    printed: list[str] = []
    for section in registered:
        if section.scope != scope:
            continue
        result = section.render(context)
        if isinstance(result, Problem):
            printed.append(
                failure.format(section=section.name, problem=result.problem, fix=result.fix)
            )
            continue
        printed.extend(
            row.format(section=section.name, name=item.name, detail=item.detail)
            for item in result.rows
            if item.state == "fired"
        )
    return tuple(printed) if printed else (empty,)
