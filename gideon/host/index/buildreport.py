"""Gather, append, and read a generation's build-report observations."""

import datetime
import json
import math
import re
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import Self

from gideon.host import agreement, caselaw, report, staging, worker
from gideon.host.index import record
from gideon.host.index.generations import GenerationInputs
from gideon.host.render.worker import (
    COURT_PATTERN,
    GENERATION_FAILURE_REASONS,
    GENERATION_PATTERN,
    GENERATION_STATES,
    WORK_ROOT,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

REPORT_STAGES = ("build", "counts", "dense", "built")
GATE_NAMES = ("accounted",)

WRITE_REPORT_SQL = """WITH payload AS (
    SELECT :'v_payload'::jsonb AS value
), written AS (
    INSERT INTO public.build_reports (
        gen_id, state, failed_stage, failure_reason, seconds,
        courts, metadata_changes, gates, wall_clock
    )
    SELECT value->>'gen_id', value->>'state', value->>'failed_stage',
           value->>'failure_reason', (value->>'seconds')::bigint,
           value->'courts', value->'metadata_changes', value->'gates',
           value->'wall_clock'
    FROM payload
    RETURNING *
)
SELECT row_to_json(written) FROM written;
"""

READ_REPORT_SQL = """SELECT row_to_json(build_report)
FROM public.build_reports AS build_report
WHERE gen_id = :'v_name'
ORDER BY written_at DESC LIMIT 1;
"""


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _court_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(COURT_PATTERN, value) is not None


@dataclass(frozen=True, slots=True)
class CourtAgreement:
    """One court's agreement counts without its work-file identity fields."""

    documents: int
    gideon_pairs: int
    map_rows: int
    map_outside: int
    map_pairs: int
    agreed: int
    gideon_only: int
    map_only_seen: int
    map_only_missed: int
    job: int
    seconds: float
    computed_at: str

    def gideon_share(self) -> int:
        """Return the agreed share of GIDEON pairs as a whole percent."""

        return round(100 * self.agreed / self.gideon_pairs) if self.gideon_pairs else 0

    def map_share(self) -> int:
        """Return the agreed share of map pairs as a whole percent."""

        return round(100 * self.agreed / self.map_pairs) if self.map_pairs else 0

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: object) -> Self | None:
        """Validate the persisted counts, timing, and agreement arithmetic."""

        names = {field.name for field in fields(cls)}
        if not isinstance(value, dict) or set(value) != names:
            return None
        if (
            any(not _count(value[name]) for name in names - {"seconds", "computed_at"})
            or value["job"] < 1
            or type(value["seconds"]) not in (int, float)
            or not math.isfinite(value["seconds"]) or value["seconds"] < 0
            or not agreement.aware_time(value["computed_at"])
            or value["agreed"] > value["gideon_pairs"]
            or value["agreed"] > value["map_pairs"]
            or value["gideon_only"] != value["gideon_pairs"] - value["agreed"]
            or value["map_only_seen"] + value["map_only_missed"]
            != value["map_pairs"] - value["agreed"]
            or value["map_outside"] > value["map_rows"]
        ):
            return None
        return cls(**value)


@dataclass(frozen=True, slots=True)
class CourtEntry:
    """The existing opinion and anchor counts beside a court's agreement."""

    counts: caselaw.CourtCounts
    anchors: caselaw.AnchorCounts
    agreement: CourtAgreement | None

    def to_json(self) -> dict[str, object]:
        return {
            **asdict(self.counts), "anchors": asdict(self.anchors),
            "agreement": self.agreement.to_json() if self.agreement is not None else None,
        }

    @classmethod
    def from_json(cls, value: object) -> Self | None:
        """Reuse the caselaw readers' validation of both count shapes."""

        count_names = {field.name for field in fields(caselaw.CourtCounts)}
        if not isinstance(value, dict) or set(value) != count_names | {"anchors", "agreement"}:
            return None
        counts = caselaw._counts_from_json({name: value[name] for name in count_names})
        anchors = caselaw._anchor_counts_from_json(value["anchors"])
        figure = CourtAgreement.from_json(value["agreement"])
        if counts is None or anchors is None or value["agreement"] is not None and figure is None:
            return None
        return cls(counts, anchors, figure)


@dataclass(frozen=True, slots=True)
class Accounted:
    """A court's recorded opinions compared with its staged opinion count."""

    staged: int | None
    recorded: int
    ready: int
    failed: int
    passed: bool | None

    def to_json(self) -> dict[str, object]:
        return {
            "staged": self.staged, "recorded": self.recorded,
            "ready": self.ready, "failed": self.failed, "pass": self.passed,
        }

    @classmethod
    def from_json(cls, value: object) -> Self | None:
        """Validate counts and the nullable completeness comparison."""

        if not isinstance(value, dict) or set(value) != {
            "staged", "recorded", "ready", "failed", "pass",
        }:
            return None
        if (
            any(not _count(value[name]) for name in ("recorded", "ready", "failed"))
            or value["staged"] is not None and not _count(value["staged"])
            or value["pass"] is not None and type(value["pass"]) is not bool
        ):
            return None
        expected = None if value["staged"] is None else value["recorded"] >= value["staged"]
        if value["pass"] is not expected:
            return None
        return cls(value["staged"], value["recorded"], value["ready"], value["failed"], value["pass"])


@dataclass(frozen=True, slots=True)
class WallClock:
    """Separate observations of the command, the halves, and the identity's life."""

    run_seconds: int
    lexical_seconds: int | None
    dense_seconds: int | None
    created_at: str
    lexical_built_at: str | None
    dense_built_at: str | None
    built_at: str | None
    lifecycle_seconds: int | None
    whole_build: bool

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: object) -> Self | None:
        """Validate nonnegative observations and aware lifecycle timestamps."""

        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            return None
        if (
            not _count(value["run_seconds"])
            or any(value[name] is not None and not _count(value[name]) for name in (
                "lexical_seconds", "dense_seconds", "lifecycle_seconds",
            ))
            or type(value["whole_build"]) is not bool
            or not agreement.aware_time(value["created_at"])
            or any(value[name] is not None and not agreement.aware_time(value[name]) for name in (
                "lexical_built_at", "dense_built_at", "built_at",
            ))
        ):
            return None
        if value["built_at"] is None:
            if value["lifecycle_seconds"] is not None:
                return None
        else:
            span = (datetime.datetime.fromisoformat(value["built_at"])
                    - datetime.datetime.fromisoformat(value["created_at"])).total_seconds()
            if span < 0 or value["lifecycle_seconds"] != int(span):
                return None
        return cls(**value)


@dataclass(frozen=True, slots=True)
class BuildReport:
    """Every column of one append-only build-report row."""

    gen_id: str
    written_at: str
    state: str
    failed_stage: str | None
    failure_reason: str | None
    seconds: int
    courts: Mapping[str, CourtEntry]
    metadata_changes: Mapping[str, object]
    gates: Mapping[str, Mapping[str, Accounted]]
    wall_clock: WallClock

    def to_json(self) -> dict[str, object]:
        return {
            "gen_id": self.gen_id, "written_at": self.written_at,
            "state": self.state, "failed_stage": self.failed_stage,
            "failure_reason": self.failure_reason, "seconds": self.seconds,
            "courts": {court: entry.to_json() for court, entry in self.courts.items()},
            "metadata_changes": dict(self.metadata_changes),
            "gates": {
                name: {court: row.to_json() for court, row in rows.items()}
                for name, rows in self.gates.items()
            },
            "wall_clock": self.wall_clock.to_json(),
        }

    @classmethod
    def from_json(cls, value: object) -> Self | None:
        """Parse a complete row, refusing unknown shapes and closed-set values."""

        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            return None
        if (
            not isinstance(value["gen_id"], str)
            or re.fullmatch(GENERATION_PATTERN, value["gen_id"]) is None
            or len(value["gen_id"].encode()) >= 255
            or not agreement.aware_time(value["written_at"])
            or not isinstance(value["state"], str) or value["state"] not in GENERATION_STATES
            or value["failed_stage"] is not None and (
                not isinstance(value["failed_stage"], str) or value["failed_stage"] not in REPORT_STAGES
            )
            or value["failure_reason"] is not None and (
                not isinstance(value["failure_reason"], str)
                or value["failure_reason"] not in GENERATION_FAILURE_REASONS
            )
            or (value["state"] == "failed") != (value["failure_reason"] is not None)
            or not _count(value["seconds"])
            or not isinstance(value["courts"], dict)
            or not isinstance(value["metadata_changes"], dict)
            or not all(isinstance(name, str) for name in value["metadata_changes"])
            or not isinstance(value["gates"], dict)
        ):
            return None
        courts: dict[str, CourtEntry] = {}
        for court, entry in value["courts"].items():
            parsed_entry = CourtEntry.from_json(entry)
            if not _court_id(court) or parsed_entry is None:
                return None
            courts[court] = parsed_entry
        gates: dict[str, dict[str, Accounted]] = {}
        for name, rows in value["gates"].items():
            if name not in GATE_NAMES or not isinstance(rows, dict):
                return None
            gates[name] = {}
            for court, row in rows.items():
                parsed_row = Accounted.from_json(row)
                if not _court_id(court) or parsed_row is None:
                    return None
                gates[name][court] = parsed_row
        wall_clock = WallClock.from_json(value["wall_clock"])
        if wall_clock is None or wall_clock.run_seconds != value["seconds"]:
            return None
        return cls(
            value["gen_id"], value["written_at"], value["state"], value["failed_stage"],
            value["failure_reason"], value["seconds"], courts, value["metadata_changes"],
            gates, wall_clock,
        )


def gather(
    host: Host, rendered_dir: PathLike, inputs: GenerationInputs,
    row: record.GenerationRow, seconds: int, failed_stage: str | None,
    whole_build: bool, *, work_root: PathLike = WORK_ROOT,
    command_path: str = "index build",
) -> BuildReport | Problem:
    """Gather record counts and optional work files without writing the database."""

    problem = staging.snapshot_problem(inputs.snapshot, command_path, subject="build report")
    if problem is not None:
        return problem
    source = inputs.snapshot[:-staging.DATE_SUFFIX_LENGTH]
    stage = staging.read_record(
        host, inputs.label, source, work_root=work_root, command_path=command_path,
    )
    if isinstance(stage, Problem):
        return stage
    # A stage of another snapshot, or without a court, has no count to compare.
    staged_counts = stage.counts if stage is not None and stage.snapshot == inputs.snapshot else {}
    courts: dict[str, CourtEntry] = {}
    accounted: dict[str, Accounted] = {}
    for court in sorted(inputs.courts):
        counts = caselaw.read_counts(
            host, rendered_dir, source=source, snapshot_date=inputs.snapshot[-10:],
            court=court, command_path=command_path,
        )
        if isinstance(counts, Problem):
            return counts
        anchors = caselaw.read_anchor_counts(
            host, rendered_dir, source=source, snapshot_date=inputs.snapshot[-10:],
            court=court, command_path=command_path,
        )
        if isinstance(anchors, Problem):
            return anchors
        figure = agreement.read_record(
            host, inputs.label, inputs.snapshot, court,
            work_root=work_root, command_path=command_path,
        )
        if isinstance(figure, Problem):
            return figure
        court_agreement = None if figure is None else CourtAgreement(**{
            field.name: getattr(figure, field.name) for field in fields(CourtAgreement)
        })
        courts[court] = CourtEntry(counts, anchors, court_agreement)
        staged = staged_counts[court]["opinions"] if court in staged_counts else None
        accounted[court] = Accounted(
            staged, counts.opinions, counts.by_status.get("ready", 0),
            counts.by_status.get("failed", 0),
            None if staged is None else counts.opinions >= staged,
        )
    fix = record.database_fix(rendered_dir, command_path)
    if not agreement.aware_time(row.created_at) or (
        row.built_at is not None and not agreement.aware_time(row.built_at)
    ):
        return Problem("build report generation timestamps are invalid", fix)
    lifecycle_seconds = None if row.built_at is None else int((
        datetime.datetime.fromisoformat(row.built_at)
        - datetime.datetime.fromisoformat(row.created_at)
    ).total_seconds())
    wall_clock = WallClock(
        seconds, row.counts.get("seconds"), row.counts.get("dense_seconds"),
        row.created_at, row.lexical_built_at, row.dense_built_at, row.built_at,
        lifecycle_seconds, whole_build,
    )
    # The gathered time stands until the write returns the database's own.
    return BuildReport(
        row.gen_id, datetime.datetime.now(datetime.UTC).isoformat(), row.state,
        failed_stage, row.failure_reason, seconds, courts, {},
        {"accounted": accounted}, wall_clock,
    )


def _run(
    host: Host, rendered_dir: PathLike, sql: str, command_path: str,
) -> subprocess.CompletedProcess[str] | Problem:
    fix = f"Restore Docker Compose and psql, then run {report.command(command_path)} again."
    try:
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"build report command could not run ({type(exc).__name__})", fix)
    if result.returncode == 127:
        return Problem("build report command is unavailable (exit 127)", fix)
    return result


def _read_row(stdout: str, name: str) -> BuildReport | None:
    try:
        row = BuildReport.from_json(json.loads(stdout))
    except ValueError:
        return None
    return row if row is not None and row.gen_id == name else None


def write_report(
    host: Host, rendered_dir: PathLike, build_report: BuildReport,
    *, command_path: str = "index build",
) -> BuildReport | Problem:
    """Append one report, returning its database-assigned write timestamp."""

    fix = record.database_fix(rendered_dir, command_path)
    payload = build_report.to_json()
    if BuildReport.from_json(payload) is None:
        return Problem("build report payload is invalid", fix)
    sql = worker.bind("v_payload", json.dumps(payload, separators=(",", ":"))) + "\n" + WRITE_REPORT_SQL
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    if result.returncode != 0:
        return Problem(f"build report write failed (exit {result.returncode})", fix)
    row = _read_row(result.stdout, build_report.gen_id)
    if row is None:
        return Problem("build report write returned an invalid row", fix)
    return row


def read_report(
    host: Host, rendered_dir: PathLike, name: str, *, command_path: str = "index build",
) -> BuildReport | None | Problem:
    """Read the newest report for a generation, or none before its first write."""

    sql = worker.bind("v_name", name) + "\n" + READ_REPORT_SQL
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = record.database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"build report read failed (exit {result.returncode})", fix)
    if not result.stdout.strip():
        return None
    row = _read_row(result.stdout, name)
    if row is None:
        return Problem("build report row is invalid", fix)
    return row


def summary(build_report: BuildReport) -> str:
    """Render the stage row with identifiers, counts, and elapsed seconds alone."""

    courts = tuple(build_report.courts.values())
    opinions = sum(court.counts.opinions for court in courts)
    ready = sum(court.counts.by_status.get("ready", 0) for court in courts)
    failed = sum(court.counts.by_status.get("failed", 0) for court in courts)
    anchored = sum(court.anchors.anchored for court in courts)
    figures = sum(court.agreement is not None for court in courts)
    accounted = tuple(build_report.gates.get("accounted", {}).values())
    staged = sum(row.staged is not None for row in accounted)
    passed = sum(row.passed is True for row in accounted)
    return (
        f"{build_report.gen_id}: written_at {build_report.written_at}; courts {len(courts)}; "
        f"opinions {opinions}, ready {ready}, failed {failed}; anchored {anchored} of {ready}; "
        f"agreement figures {figures} of {len(courts)} courts; accounted {passed} of {staged} staged; "
        f"seconds {build_report.seconds}"
    )
