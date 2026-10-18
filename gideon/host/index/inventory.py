"""Print the corpus lockfiles and index generations held by the record."""

import argparse
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from gideon.host import report
from gideon.host.corpus.record import CutRow, read_lockfiles
from gideon.host.index import buildreport, generations, record
from gideon.host.index.treatment import RISK_STATES
from gideon.host.render.worker import (
    GENERATION_PATTERN,
    PRECEDENTIAL_VALUES,
    TEXT_SOURCES,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike, RealHost

COMMAND_PATH = "index report"
HEADERS = ("lockfiles", "generations", "inventory")
STATUS_ORDER = ("ready", "processing", "failed", "withdrawn")


@dataclass(frozen=True, slots=True)
class Totals:
    """Opinion distributions, anchor passes, and coverage summed across courts."""

    opinions: int
    ready: int
    failed: int
    anchored: int
    anchor_ready: int
    with_anchors: int
    documents: int
    by_status: Mapping[str, int]
    by_text_source: Mapping[str, int]
    by_precedential: Mapping[str, int]
    by_failure_reason: Mapping[str, int]


def _sum_counts(maps: Iterable[Mapping[str, int]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for values in maps:
        for key, count in values.items():
            counts[key] = counts.get(key, 0) + count
    return counts


def totals_for(courts: Mapping[str, buildreport.CourtEntry]) -> Totals:
    """Sum the report's recorded figures without conflating pass and coverage."""

    entries = tuple(courts.values())
    coverage = tuple(source for entry in entries for source in entry.anchors.by_text_source.values())
    status = _sum_counts(entry.counts.by_status for entry in entries)
    return Totals(
        sum(entry.counts.opinions for entry in entries), status.get("ready", 0),
        status.get("failed", 0), sum(entry.anchors.anchored for entry in entries),
        sum(entry.anchors.ready for entry in entries),
        sum(source.with_anchors for source in coverage), sum(source.documents for source in coverage),
        status, _sum_counts(entry.counts.by_text_source for entry in entries),
        _sum_counts(entry.counts.by_precedential for entry in entries),
        _sum_counts(entry.counts.by_failure_reason for entry in entries),
    )


def _percent(numerator: int, denominator: int) -> int:
    return round(100 * numerator / denominator) if denominator else 0


def _ordered_keys(values: Mapping[str, object], order: Sequence[str]) -> tuple[str, ...]:
    return (*order, *sorted(set(values) - set(order)))


def _counts_text(counts: Mapping[str, int], order: Sequence[str] = ()) -> str:
    return ", ".join(f"{key} {counts.get(key, 0)}" for key in _ordered_keys(counts, order)) or "none"


def _print_line(value: object) -> None:
    print(report.one_line(value))


def _print_lockfiles(rows: Sequence[CutRow]) -> None:
    _print_line(HEADERS[0])
    if not rows:
        _print_line("none")
        return
    superseded = 0
    for row in sorted(rows, key=lambda row: row.label):
        if row.state not in {"cut", "installing", "installed"}:
            superseded += 1
            continue
        sources = ", ".join(f"{pin.source} {pin.snapshot_date}" for pin in row.sources)
        _print_line(
            f"{row.label}: {row.state}; pipeline {row.pipeline}; cut {row.cut_at}; "
            f"installed {row.installed_at or 'not installed'}; base {row.base or 'none'}; "
            f"sources {sources}"
        )
    _print_line(f"superseded {superseded}")


def _print_generations(rows: Sequence[record.GenerationRow]) -> None:
    _print_line(HEADERS[1])
    if not rows:
        _print_line("none")
    for row in rows:
        state = row.state
        if row.failure_reason is not None:
            state += f", {row.failure_reason}"
        line = (
            f"{row.gen_id}: {state}; {row.collection}; lockfiles {', '.join(row.lockfiles)}; "
            f"documents ready {row.counts.get('ready', 0)}; "
            f"chunks {row.counts.get('indexed', 0) + row.counts.get('present', 0)}; "
            f"bytes {row.bytes}; created {row.created_at}; "
            f"lexical built {row.lexical_built_at or 'not built'}; "
            f"dense built {row.dense_built_at or 'not built'}"
        )
        if row.promoted_at is not None:
            line += f"; promoted {row.promoted_at}"
        if row.retired_at is not None:
            line += f"; retired {row.retired_at}"
        _print_line(line)


def _print_court(court: str, entry: buildreport.CourtEntry, accounted: buildreport.Accounted | None) -> None:
    counts = entry.counts
    _print_line(f"{court}: opinions {counts.opinions}; by status {_counts_text(counts.by_status, STATUS_ORDER)}")
    _print_line(f"{court}: by text source {_counts_text(counts.by_text_source, TEXT_SOURCES)}")
    _print_line(f"{court}: by precedential {_counts_text(counts.by_precedential, PRECEDENTIAL_VALUES)}")
    _print_line(f"{court}: by failure reason {_counts_text(counts.by_failure_reason)}")
    anchors = entry.anchors
    coverage = tuple(anchors.by_text_source.values())
    documents = sum(source.documents for source in coverage)
    with_anchors = sum(source.with_anchors for source in coverage)
    chars = sum(source.chars for source in coverage)
    anchored_chars = sum(source.anchored_chars for source in coverage)
    by_source = ", ".join(
        f"{source} {value.with_anchors} of {value.documents} ({_percent(value.with_anchors, value.documents)}%)"
        for source in _ordered_keys(anchors.by_text_source, TEXT_SOURCES)
        if (value := anchors.by_text_source.get(source)) is not None
    ) or "none"
    _print_line(
        f"{court}: anchor pass {anchors.anchored} of {anchors.ready}; "
        f"with anchors {with_anchors} of {documents} ({_percent(with_anchors, documents)}%); "
        f"anchored chars {_percent(anchored_chars, chars)}%; by text source {by_source}"
    )
    figure = entry.agreement
    if figure is None:
        _print_line(f"{court}: agreement none")
    else:
        _print_line(
            f"{court}: agreement {figure.gideon_share()}% of GIDEON pairs, "
            f"{figure.map_share()}% of map pairs; job {figure.job}; computed {figure.computed_at}"
        )
    if accounted is None:
        _print_line(f"{court}: accounted none")
    else:
        outcome = "not staged" if accounted.passed is None else "pass" if accounted.passed else "fail"
        staged = "none" if accounted.staged is None else accounted.staged
        _print_line(
            f"{court}: accounted staged {staged}, recorded {accounted.recorded}, "
            f"ready {accounted.ready}, failed {accounted.failed}, {outcome}"
        )


def _print_inventory(row: record.GenerationRow, build_report: buildreport.BuildReport | None) -> None:
    _print_line(f"{HEADERS[2]} {row.gen_id}")
    _print_line(
        f"{row.gen_id}: {row.state}; {row.collection}; lockfile {row.corpus_lockfile}, "
        f"served {', '.join(row.lockfiles)}; pipeline {row.pipeline}; "
        f"embedding space {row.embedding_space}"
    )
    counts = row.counts
    _print_line(
        f"lexical: documents ready {counts.get('ready', 0)}, chunked {counts.get('chunked', 0)}, "
        f"unchunked {counts.get('unchunked', 0)}; chunks indexed {counts.get('indexed', 0)}, "
        f"present {counts.get('present', 0)}, leveled {counts.get('leveled', 0)}; "
        f"bytes {row.bytes}; built {row.lexical_built_at or 'not built'}"
    )
    _print_line(
        f"dense: points {counts.get('dense_points', 0)}, indexed {counts.get('dense_indexed', 0)}, "
        f"written {counts.get('dense_copied', 0) + counts.get('dense_embedded', 0)} "
        f"(present {counts.get('dense_present', 0)}, leveled {counts.get('dense_leveled', 0)}, "
        f"copied {counts.get('dense_copied', 0)}, "
        f"embedded {counts.get('dense_embedded', 0)}), tokens {counts.get('dense_tokens', 0)}; "
        f"built {row.dense_built_at or 'not built'}"
    )
    if "cluster_chunks" not in counts:
        _print_line("clustering: none")
    else:
        chunks = counts["cluster_chunks"]
        flagged = counts.get("cluster_flagged", 0)
        _print_line(
            f"clustering: clusters {counts.get('cluster_clusters', 0)} over "
            f"{counts.get('cluster_grouped', 0)} chunks of {chunks} "
            f"({counts.get('cluster_members', 0)} near-duplicate texts of {counts.get('cluster_texts', 0)}); "
            f"boilerplate {counts.get('cluster_boilerplate', 0)} clusters over {flagged} chunks "
            f"({_percent(flagged, chunks)}% of {chunks}); seconds {counts.get('cluster_seconds', 0)}; "
            f"reused {'yes' if counts.get('cluster_reused', 0) == 1 else 'no'}; "
            f"clustered {row.clustered_at or 'not clustered'}"
        )
    if build_report is None:
        line = "report: none; no build report is recorded for this generation"
        if row.state in {"building", "built", "failed"}:
            line += (
                f"; {report.command(f'index build {row.collection} --lockfile {row.corpus_lockfile}')} "
                "writes one when the lockfile, pipeline, and embedding space still name it"
            )
        else:
            line += ", which predates the report or whose runs never reached its generation stage"
        _print_line(line)
        return
    wall_clock = build_report.wall_clock
    lifecycle = "not built" if wall_clock.lifecycle_seconds is None else wall_clock.lifecycle_seconds
    _print_line(
        f"report: written {build_report.written_at}; state {build_report.state}; "
        f"failed stage {build_report.failed_stage or 'none'}; reason {build_report.failure_reason or 'none'}; "
        f"seconds {build_report.seconds}; whole build {'yes' if wall_clock.whole_build else 'no'}; "
        f"lifecycle seconds {lifecycle}"
    )
    cluster = build_report.gates.get("cluster")
    if cluster is None:
        _print_line("gate cluster: none")
    else:
        _print_line(
            f"gate cluster: seconds {cluster.seconds} of budget {cluster.budget}; "
            f"reused {'yes' if cluster.reused else 'no'}; {'pass' if cluster.passed else 'fail'}"
        )
    treatment = build_report.treatment
    if treatment is None:
        _print_line("treatment: none")
    else:
        states = ", ".join(
            f"{state} {treatment.by_state[state]} ({_percent(treatment.by_state[state], treatment.clusters)}%)"
            for state in RISK_STATES
        )
        _print_line(
            f"treatment: clusters {treatment.clusters}; {states}; materialized {treatment.materialized_at}"
        )
    totals = totals_for(build_report.courts)
    _print_line(
        f"courts {len(build_report.courts)}: opinions {totals.opinions}; ready {totals.ready}; "
        f"failed {totals.failed}; anchor pass {totals.anchored} of {totals.anchor_ready}; "
        f"with anchors {totals.with_anchors} of {totals.documents} "
        f"({_percent(totals.with_anchors, totals.documents)}%)"
    )
    _print_line(f"by status: {_counts_text(totals.by_status, STATUS_ORDER)}")
    _print_line(f"by text source: {_counts_text(totals.by_text_source, TEXT_SOURCES)}")
    _print_line(f"by precedential: {_counts_text(totals.by_precedential, PRECEDENTIAL_VALUES)}")
    _print_line(f"by failure reason: {_counts_text(totals.by_failure_reason)}")
    for court, entry in sorted(build_report.courts.items()):
        _print_court(court, entry, build_report.gates.get("accounted", {}).get(court))
    changes = ", ".join(
        f"{field} {report.one_line(count) if type(count) is int else 'present'}"
        for field, count in sorted(build_report.metadata_changes.items())
    ) or "none"
    _print_line(f"metadata changes: {changes}")


def run_index_report(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered",
    collections: Sequence[generations.Collection] = generations.COLLECTIONS,
) -> int:
    """Print recorded lockfiles, generations, and the selected inventories."""

    io = RealHost() if host is None else host
    if io.geteuid() != 0:
        print(report.refusal(
            COMMAND_PATH, "root privileges are required.",
            f"Run {report.command(COMMAND_PATH)}.",
        ), file=sys.stderr)
        return 2

    name = args.generation
    listing_fix = (
        f"Run {report.command(COMMAND_PATH)} to list the recorded generations, then name one."
    )
    if name is not None and (
        re.fullmatch(GENERATION_PATTERN, name) is None or len(name.encode()) >= 255
    ):
        print(report.refusal(COMMAND_PATH, "generation name is invalid", listing_fix), file=sys.stderr)
        return 2

    lockfiles = read_lockfiles(io, rendered_dir, command_path=COMMAND_PATH)
    if isinstance(lockfiles, Problem):
        print(report.refusal(COMMAND_PATH, lockfiles.problem, lockfiles.fix), file=sys.stderr)
        return 2
    rows = record.read_generations(io, rendered_dir, command_path=COMMAND_PATH)
    if isinstance(rows, Problem):
        print(report.refusal(COMMAND_PATH, rows.problem, rows.fix), file=sys.stderr)
        return 2
    subject = next((row for row in rows if row.gen_id == name), None)
    if name is not None and subject is None:
        print(report.refusal(
            COMMAND_PATH, f"generation {name} is not recorded", listing_fix,
        ), file=sys.stderr)
        return 2

    _print_lockfiles(lockfiles)
    _print_generations(rows)
    subjects: list[record.GenerationRow | str] = [subject] if subject is not None else []
    if subject is None:
        for collection in collections:
            live = [row for row in rows if row.collection == collection.name and row.state == "live"]
            if live:
                subjects.extend(live)
            else:
                subjects.append(
                    f"{HEADERS[2]} {collection.name}: no live generation; name a generation to report it"
                )
    for item in subjects:
        if isinstance(item, str):
            _print_line(item)
            continue
        build_report = buildreport.read_report(io, rendered_dir, item.gen_id, command_path=COMMAND_PATH)
        if isinstance(build_report, Problem):
            print(report.refusal(COMMAND_PATH, build_report.problem, build_report.fix), file=sys.stderr)
            return 2
        _print_inventory(item, build_report)
    return 0
