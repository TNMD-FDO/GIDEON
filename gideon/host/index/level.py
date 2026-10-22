"""Level restored derived stores with the generation record and its live aliases."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

from gideon.host import derivedstores, report
from gideon.host.index import aliases, record
from gideon.host.render.opensearch import OPENSEARCH_PASSWORD_SECRET_NAME
from gideon.host.render.qdrant import QDRANT_SECRET_NAME
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

_NO_GENERATIONS = "level: no generation recorded"
_BUILDING_CLAUSE = "level {name}: building, left to its resume"
_LEFT_CLAUSE = "level {name}: {state}, left"
_ROWLESS_CLAUSE = "level {name}: no row, left"
_UNREADABLE_CLAUSE = "level {name}: {state}, counts unreadable, left"
_WHOLE_CLAUSE = (
    "level {name}: {state}, whole (opensearch {lexical_count} of {lexical_expected}, "
    "qdrant {dense_count} of {dense_expected})"
)
_ALIASED_CLAUSE = _WHOLE_CLAUSE + ", alias lexical {lexical_alias}, dense {dense_alias}"
_DEMOTED_CLAUSE = "level {name}: {state} -> building, {lexical_reading}, {dense_reading}{standing}"
_ABSENT_CLAUSE = "level {name}: absent in qdrant and opensearch, now building{standing}"
_STORE_WHOLE = "whole in {store} ({count} of {expected})"
_STORE_SHORT = "short in {store} ({count} of {expected})"
_STORE_ABSENT = "absent in {store} (expected {expected})"
_ALIAS_STANDS = ", alias {store} stands"


@dataclass(frozen=True, slots=True)
class LevelOutcome:
    """One generation's prior state, store readings, and levelling outcome."""

    name: str
    collection: str | None
    state: str
    outcome: Literal["aliased", "whole", "demoted", "left"]
    lexical_count: int | None
    lexical_expected: int | None
    dense_count: int | None
    dense_expected: int | None
    lexical_alias: str | None
    dense_alias: str | None
    corpus_lockfile: str | None


@dataclass(frozen=True, slots=True)
class LevelReport:
    """Ordered outcomes and the builds and promotions needed after a restore."""

    outcomes: tuple[LevelOutcome, ...]
    next_builds: tuple[tuple[str, str], ...]
    next_promotes: tuple[str, ...]


def level_all(
    io: Host, rendered_dir: PathLike, *,
    recovered: Sequence[derivedstores.StoreRecovery], command_path: str,
) -> LevelReport | Problem:
    """Read every store before demoting short rows, then point live whole aliases."""

    rows = record.read_generations(io, rendered_dir, command_path=command_path)
    if isinstance(rows, Problem):
        return rows
    if not rows:
        return LevelReport((), (), ())

    live_collections: set[str] = set()
    for row in rows:
        if row.state == "live":
            if row.collection in live_collections:
                return Problem(
                    f"collection {row.collection} has multiple live generations",
                    f"Run {report.command('index report')} and restore a single live generation "
                    f"for {row.collection}, then run {report.command(command_path)} again.",
                )
            live_collections.add(row.collection)

    targets: dict[str, aliases.AliasTargets] = {}
    for row in rows:
        if row.state in ("built", "live") and row.collection not in targets:
            current = aliases.read_aliases(io, rendered_dir, row.collection, command_path=command_path)
            if isinstance(current, Problem):
                return current
            targets[row.collection] = current

    outcomes: list[LevelOutcome] = []
    password: str | None = None
    key: str | None = None
    for row in rows:
        lexical_expected = (
            row.counts["indexed"] + row.counts["present"]
            if "indexed" in row.counts and "present" in row.counts else None
        )
        dense_expected = row.counts.get("dense_total")
        outcome = LevelOutcome(
            row.gen_id, row.collection, row.state, "left", None, lexical_expected,
            None, dense_expected, None, None, row.corpus_lockfile,
        )
        if row.state in ("built", "live") and lexical_expected is not None and dense_expected is not None:
            if password is None:
                secret = derivedstores.store_secret(io, OPENSEARCH_PASSWORD_SECRET_NAME)
                if isinstance(secret, Problem):
                    return secret
                password = secret
            if key is None:
                secret = derivedstores.store_secret(io, QDRANT_SECRET_NAME)
                if isinstance(secret, Problem):
                    return secret
                key = secret
            lexical = derivedstores.opensearch_document_count(io, rendered_dir, password, row.gen_id)
            if isinstance(lexical, Problem):
                return lexical
            dense = derivedstores.qdrant_point_count(io, rendered_dir, key, row.gen_id)
            if isinstance(dense, Problem):
                return dense
            short = lexical is None or lexical < lexical_expected or dense is None or dense < dense_expected
            current = targets[row.collection]
            outcome = replace(
                outcome, lexical_count=lexical, dense_count=dense,
                outcome="demoted" if short else "aliased" if row.state == "live" else "whole",
                lexical_alias="stands" if short and row.gen_id in current.lexical else None,
                dense_alias="stands" if short and row.gen_id in current.dense else None,
            )
        outcomes.append(outcome)

    known = {row.gen_id for row in rows}
    for recovery in recovered:
        for name in recovery.names:
            if name not in known:
                outcomes.append(LevelOutcome(
                    name, None, "none", "left", None, None, None, None, None, None, None,
                ))
                known.add(name)

    builds: list[tuple[str, str]] = []
    promotes: list[str] = []
    for outcome in outcomes:
        if outcome.outcome != "demoted":
            continue
        assert outcome.lexical_expected is not None and outcome.dense_expected is not None
        demoted = record.demote_generation(
            io, rendered_dir, outcome.name,
            lexical_short=outcome.lexical_count is None or outcome.lexical_count < outcome.lexical_expected,
            dense_short=outcome.dense_count is None or outcome.dense_count < outcome.dense_expected,
            command_path=command_path,
        )
        if isinstance(demoted, Problem):
            return demoted
        assert outcome.collection is not None and outcome.corpus_lockfile is not None
        build = (outcome.collection, outcome.corpus_lockfile)
        if build not in builds:
            builds.append(build)
        if outcome.state == "live":
            promotes.append(outcome.name)

    for index, outcome in enumerate(outcomes):
        if outcome.outcome != "aliased":
            continue
        assert outcome.collection is not None
        current = targets[outcome.collection]
        lexical_alias = aliases.point_alias(
            io, rendered_dir, "lexical", outcome.collection, outcome.name, current.lexical,
            command_path=command_path,
        )
        if isinstance(lexical_alias, Problem):
            return lexical_alias
        dense_alias = aliases.point_alias(
            io, rendered_dir, "dense", outcome.collection, outcome.name, current.dense,
            command_path=command_path,
        )
        if isinstance(dense_alias, Problem):
            return dense_alias
        outcomes[index] = replace(outcome, lexical_alias=lexical_alias, dense_alias=dense_alias)
    return LevelReport(tuple(outcomes), tuple(builds), tuple(promotes))


def _store_reading(store: str, count: int | None, expected: int | None) -> str:
    assert expected is not None
    template = _STORE_ABSENT if count is None else _STORE_SHORT if count < expected else _STORE_WHOLE
    return template.format(store=store, count=count, expected=expected)


def level_clause(outcome: LevelOutcome) -> str:
    """Describe one outcome without the row's clause separator."""

    values = asdict(outcome)
    if outcome.outcome == "left":
        template = (
            _ROWLESS_CLAUSE if outcome.state == "none" else
            _BUILDING_CLAUSE if outcome.state == "building" else
            _UNREADABLE_CLAUSE if outcome.state in ("built", "live") else _LEFT_CLAUSE
        )
    elif outcome.outcome == "whole":
        template = _WHOLE_CLAUSE
    elif outcome.outcome == "aliased":
        template = _ALIASED_CLAUSE
    else:
        template = (
            _ABSENT_CLAUSE if outcome.lexical_count is None and outcome.dense_count is None
            else _DEMOTED_CLAUSE
        )
        values.update(
            lexical_reading=_store_reading("opensearch", outcome.lexical_count, outcome.lexical_expected),
            dense_reading=_store_reading("qdrant", outcome.dense_count, outcome.dense_expected),
            standing="".join(
                _ALIAS_STANDS.format(store=store)
                for store, result in (("lexical", outcome.lexical_alias), ("dense", outcome.dense_alias))
                if result == "stands"
            ),
        )
    return template.format(**values)


def level_clauses(result: LevelReport) -> tuple[str, ...]:
    """Describe every outcome in order, or the empty record in one clause."""

    return tuple(level_clause(outcome) for outcome in result.outcomes) or (_NO_GENERATIONS,)


def level_clause_patterns() -> tuple[re.Pattern[str], ...]:
    """Derive full-clause matchers from the same templates the restore row uses."""

    def expression(template: str, **fields: str) -> str:
        escaped = re.escape(template)
        for field, pattern in fields.items():
            escaped = escaped.replace(re.escape("{" + field + "}"), pattern)
        return escaped

    name = r"[A-Za-z0-9._-]+"
    alias = rf"(?:unchanged|(?:none|{name}(?:, {name})*) -> {name})"
    standing = "".join(
        "(?:" + expression(_ALIAS_STANDS, store=store) + ")?"
        for store in ("lexical", "dense")
    )
    readings = {
        field: "(?:" + "|".join(
            expression(template, store=store, count=r"\d+", expected=r"\d+")
            for template in (_STORE_WHOLE, _STORE_SHORT, _STORE_ABSENT)
        ) + ")"
        for field, store in (("lexical_reading", "opensearch"), ("dense_reading", "qdrant"))
    }
    counts = dict.fromkeys(("lexical_count", "lexical_expected", "dense_count", "dense_expected"), r"\d+")
    return tuple(re.compile(pattern) for pattern in (
        expression(_NO_GENERATIONS),
        expression(_BUILDING_CLAUSE, name=name),
        expression(_LEFT_CLAUSE, name=name, state="(?:retired|failed)"),
        expression(_ROWLESS_CLAUSE, name=name),
        expression(_UNREADABLE_CLAUSE, name=name, state="(?:built|live)"),
        expression(_WHOLE_CLAUSE, name=name, state="built", **counts),
        expression(_ALIASED_CLAUSE, name=name, state="live", lexical_alias=alias, dense_alias=alias, **counts),
        expression(_DEMOTED_CLAUSE, name=name, state="(?:built|live)", standing=standing, **readings),
        expression(_ABSENT_CLAUSE, name=name, standing=standing),
    ))


def level_audit(result: LevelReport) -> Mapping[str, object]:
    """Return ids, lifecycle outcomes, and store counts without clause prose."""

    return {
        outcome.name: {
            "state": outcome.state, "outcome": outcome.outcome,
            "lexical_count": outcome.lexical_count, "lexical_expected": outcome.lexical_expected,
            "dense_count": outcome.dense_count, "dense_expected": outcome.dense_expected,
            "lexical_alias": outcome.lexical_alias, "dense_alias": outcome.dense_alias,
        }
        for outcome in result.outcomes
    }


def next_lines(result: LevelReport) -> tuple[str, ...]:
    """Render builds with their recorded lockfiles, followed by required promotions."""

    return (
        *(report.command(f"index build {collection} --lockfile {label}") for collection, label in result.next_builds),
        *(report.command(f"index promote {name}") for name in result.next_promotes),
    )
