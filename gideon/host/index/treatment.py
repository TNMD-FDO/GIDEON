"""Materialize a generation's cluster risk states and validate their distribution."""

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Self

from gideon.host import agreement, caselaw, report, staging, worker
from gideon.host.index import record
from gideon.host.index.generations import GenerationInputs
from gideon.host.render.worker import (
    NO_STATE_REASONS,
    OVERRULED_SOURCE,
    PATTERN_SET_ID,
    SIGNAL_PATTERN,
    TREATMENT_SIGNALS,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

# Release switch: the captured known_overruled rows set negative by the official list.
SIGNAL_LIST: Final[bool] = True
RISK_STATES: Final[tuple[str, ...]] = ("negative", "caution", "unchecked")
DISTRIBUTION_KEYS: Final = frozenset({
    "materialized_at", "pattern_set", "signal_pattern", "signal_list",
    "treatment_snapshot", "clusters", "by_state", "by_source", "by_signal",
    "outside", "by_no_state_reason",
})
PATTERN_ELEMENT_KEYS: Final = frozenset({
    "source", "pattern_set", "signal", "qualifier", "state", "section",
    "citation_id", "doc_id", "citing_cluster", "court", "year",
})
LIST_ELEMENT_KEYS: Final = frozenset({
    "source", "row_id", "signal", "qualifier", "state", "overruling_name",
    "overruling_cite", "overruling_docket", "overruling_year",
    "overruling_cluster", "overruling_resolution",
})
BY_SOURCE_KEYS: Final = frozenset({"pattern", "list"})
BY_SIGNAL_KEYS: Final = frozenset(TREATMENT_SIGNALS)
BY_NO_STATE_REASON_KEYS: Final = frozenset(NO_STATE_REASONS)
# A persisted report keeps the set it was derived under, so any set's id parses.
PATTERN_SET_PATTERN: Final = r"treatment/patterns@[1-9][0-9]*"

MATERIALIZE_SQL = """\\set QUIET on
BEGIN;
WITH payload AS (
    SELECT :'v_payload'::jsonb AS value
)
DELETE FROM public.cluster_treatment
WHERE gen_id = (SELECT value->>'gen_id' FROM payload);

WITH payload AS (
    SELECT :'v_payload'::jsonb AS value
), ready_documents AS (
    SELECT d.doc_id, o.cluster_id, o.court, o.decided_date
    FROM public.documents AS d
    JOIN public.opinions AS o ON o.doc_id = d.doc_id
    CROSS JOIN payload AS p
    WHERE d.source = p.value->>'source'
      AND d.source_snapshot = (p.value->>'snapshot_date')::date
      AND d.status = 'ready'
      AND o.court IN (SELECT jsonb_array_elements_text(p.value->'courts'))
), held AS (
    SELECT DISTINCT cluster_id FROM ready_documents
), selected_signals AS (
    SELECT s.*, c.to_cluster, d.cluster_id AS citing_cluster,
           d.court, d.decided_date
    FROM public.citation_signals AS s
    JOIN public.citations AS c ON c.citation_id = s.citation_id
    JOIN ready_documents AS d ON d.doc_id = s.doc_id
    CROSS JOIN payload AS p
    WHERE s.pattern_set = p.value->>'pattern_set'
      AND s.signal_source = 'pattern'
), pattern AS (
    SELECT s.to_cluster AS cluster_id, s.state, 1 AS arm,
           s.citation_id AS element_id,
           jsonb_build_object(
               'source', 'pattern', 'pattern_set', s.pattern_set,
               'signal', s.treatment_signal, 'qualifier', s.qualifier,
               'state', s.state, 'section', s.effective_section,
               'citation_id', s.citation_id, 'doc_id', s.doc_id,
               'citing_cluster', s.citing_cluster, 'court', s.court,
               'year', extract(year FROM s.decided_date)::integer
           ) AS element
    FROM selected_signals AS s CROSS JOIN payload AS p
    WHERE (p.value->>'signal_pattern')::boolean
      AND s.state IS NOT NULL AND s.to_cluster IS NOT NULL
), list AS (
    SELECT k.overruled_cluster AS cluster_id, 'negative'::text AS state,
           0 AS arm, k.row_id AS element_id,
           jsonb_build_object(
               'source', 'list', 'row_id', k.row_id, 'signal', 'overruled',
               'qualifier', k.qualifier, 'state', 'negative',
               'overruling_name', k.overruling_name,
               'overruling_cite', k.overruling_cite,
               'overruling_docket', k.overruling_docket,
               'overruling_year', k.overruling_year,
               'overruling_cluster', k.overruling_cluster,
               'overruling_resolution', k.overruling_resolution
           ) AS element
    FROM public.known_overruled AS k CROSS JOIN payload AS p
    WHERE (p.value->>'signal_list')::boolean
      AND p.value->>'treatment_snapshot_date' IS NOT NULL
      AND k.source = p.value->>'treatment_source'
      AND k.source_snapshot = (p.value->>'treatment_snapshot_date')::date
      AND k.caselaw_source = p.value->>'source'
      AND k.caselaw_snapshot = (p.value->>'snapshot_date')::date
      AND k.overruled_cluster IS NOT NULL
), candidates AS (
    SELECT * FROM pattern UNION ALL SELECT * FROM list
), kept AS (
    SELECT c.* FROM candidates AS c JOIN held AS h USING (cluster_id)
), written AS (
    INSERT INTO public.cluster_treatment (
        gen_id, cluster_id, state, signals, materialized_at
    )
    SELECT p.value->>'gen_id', k.cluster_id,
           CASE WHEN bool_or(k.state = 'negative') THEN 'negative' ELSE 'caution' END,
           jsonb_agg(k.element ORDER BY k.arm, k.element_id), now()
    FROM kept AS k CROSS JOIN payload AS p
    GROUP BY p.value->>'gen_id', k.cluster_id
    RETURNING state
)
SELECT jsonb_build_object(
    'materialized_at', now(), 'pattern_set', p.value->>'pattern_set',
    'signal_pattern', (p.value->>'signal_pattern')::boolean,
    'signal_list', (p.value->>'signal_list')::boolean,
    'treatment_snapshot', CASE WHEN p.value->>'treatment_source' IS NOT NULL
        THEN (p.value->>'treatment_source') || '-' || (p.value->>'treatment_snapshot_date')
        ELSE NULL END,
    'clusters', (SELECT count(*) FROM held),
    'by_state', jsonb_build_object(
        'negative', (SELECT count(*) FROM written WHERE state = 'negative'),
        'caution', (SELECT count(*) FROM written WHERE state = 'caution'),
        'unchecked', (SELECT count(*) FROM held) - (SELECT count(*) FROM written)
    ),
    'by_source', COALESCE((
        SELECT jsonb_object_agg(source, n) FROM (
            SELECT element->>'source' AS source, count(*) AS n
            FROM kept GROUP BY element->>'source'
        ) AS counts
    ), '{}'::jsonb),
    'by_signal', COALESCE((
        SELECT jsonb_object_agg(signal, n) FROM (
            SELECT element->>'signal' AS signal, count(*) AS n
            FROM kept GROUP BY element->>'signal'
        ) AS counts
    ), '{}'::jsonb),
    'outside', (SELECT count(*) FROM candidates AS c
                WHERE NOT EXISTS (SELECT 1 FROM held AS h WHERE h.cluster_id = c.cluster_id)),
    'by_no_state_reason', COALESCE((
        SELECT jsonb_object_agg(no_state_reason, n) FROM (
            SELECT no_state_reason, count(*) AS n FROM selected_signals
            WHERE state IS NULL GROUP BY no_state_reason
        ) AS counts
    ), '{}'::jsonb)
)
FROM payload AS p;
COMMIT;
"""


@dataclass(frozen=True, slots=True)
class Treatment:
    """A persisted risk-state distribution and the release inputs that derived it."""

    materialized_at: str
    pattern_set: str
    signal_pattern: bool
    signal_list: bool
    treatment_snapshot: str | None
    clusters: int
    by_state: Mapping[str, int]
    by_source: Mapping[str, int]
    by_signal: Mapping[str, int]
    outside: int
    by_no_state_reason: Mapping[str, int]

    def to_json(self) -> dict[str, object]:
        """Return the closed JSON object stored beside a build report."""

        return {
            "materialized_at": self.materialized_at, "pattern_set": self.pattern_set,
            "signal_pattern": self.signal_pattern, "signal_list": self.signal_list,
            "treatment_snapshot": self.treatment_snapshot, "clusters": self.clusters,
            "by_state": dict(self.by_state), "by_source": dict(self.by_source),
            "by_signal": dict(self.by_signal), "outside": self.outside,
            "by_no_state_reason": dict(self.by_no_state_reason),
        }

    @classmethod
    def from_json(cls, value: object) -> Self | None:
        """Validate a distribution, including one persisted under an older pattern set."""

        if not isinstance(value, dict) or set(value) != DISTRIBUTION_KEYS:
            return None
        if (
            not agreement.aware_time(value["materialized_at"])
            or not isinstance(value["pattern_set"], str)
            or re.fullmatch(PATTERN_SET_PATTERN, value["pattern_set"]) is None
            or type(value["signal_pattern"]) is not bool
            or type(value["signal_list"]) is not bool
            or any(type(value[key]) is not int or value[key] < 0 for key in ("clusters", "outside"))
        ):
            return None
        snapshot = value["treatment_snapshot"]
        if snapshot is not None and (
            staging.snapshot_problem(snapshot, "index build", subject="treatment") is not None
            or snapshot[:-staging.DATE_SUFFIX_LENGTH] != OVERRULED_SOURCE
        ):
            return None
        by_state = caselaw.counts_map(value["by_state"], frozenset(RISK_STATES))
        by_source = caselaw.counts_map(value["by_source"], BY_SOURCE_KEYS)
        by_signal = caselaw.counts_map(value["by_signal"], BY_SIGNAL_KEYS)
        reasons = caselaw.counts_map(value["by_no_state_reason"], BY_NO_STATE_REASON_KEYS)
        if by_state is None or by_source is None or by_signal is None or reasons is None:
            return None
        if set(by_state) != set(RISK_STATES) or sum(by_state.values()) != value["clusters"]:
            return None
        return cls(
            value["materialized_at"], value["pattern_set"], value["signal_pattern"],
            value["signal_list"], snapshot, value["clusters"], by_state, by_source,
            by_signal, value["outside"], reasons,
        )


def _run(
    host: Host, rendered_dir: PathLike, sql: str, command_path: str,
) -> subprocess.CompletedProcess[str] | Problem:
    fix = f"Restore Docker Compose and psql, then run {report.command(command_path)} again."
    try:
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"treatment command could not run ({type(exc).__name__})", fix)
    if result.returncode == 127:
        return Problem("treatment command is unavailable (exit 127)", fix)
    return result


def materialize(
    host: Host, rendered_dir: PathLike, inputs: GenerationInputs,
    treatment_snapshot: str | None, *, command_path: str = "index build",
) -> Treatment | Problem:
    """Replace a generation's derived rows atomically and read their distribution."""

    problem = staging.snapshot_problem(inputs.snapshot, command_path, subject="treatment case-law")
    if problem is not None:
        return problem
    if treatment_snapshot is not None:
        problem = staging.snapshot_problem(treatment_snapshot, command_path, subject="treatment")
        if problem is not None:
            return problem
        if treatment_snapshot[:-staging.DATE_SUFFIX_LENGTH] != OVERRULED_SOURCE:
            return Problem(
                "treatment snapshot source is invalid",
                f"Use a {OVERRULED_SOURCE}-YYYY-MM-DD snapshot, then run "
                f"{report.command(command_path)} again.",
            )
    payload = {
        "gen_id": inputs.name, "source": inputs.snapshot[:-staging.DATE_SUFFIX_LENGTH],
        "snapshot_date": inputs.snapshot[-10:], "courts": list(inputs.courts),
        "pattern_set": PATTERN_SET_ID, "signal_pattern": SIGNAL_PATTERN,
        "signal_list": SIGNAL_LIST,
        "treatment_source": (
            treatment_snapshot[:-staging.DATE_SUFFIX_LENGTH] if treatment_snapshot is not None else None
        ),
        "treatment_snapshot_date": treatment_snapshot[-10:] if treatment_snapshot is not None else None,
    }
    sql = worker.bind("v_payload", json.dumps(payload, separators=(",", ":"))) + "\n" + MATERIALIZE_SQL
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = record.database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"treatment materialization failed (exit {result.returncode})", fix)
    try:
        treatment = Treatment.from_json(json.loads(result.stdout))
    except ValueError:
        treatment = None
    if treatment is None:
        return Problem("treatment materialization returned an invalid row", fix)
    if treatment.pattern_set != payload["pattern_set"]:
        return Problem("treatment materialization returned a different pattern set", fix)
    return treatment


def summary(treatment: Treatment) -> str:
    """Render the materialization's timestamp, release inputs, and counts alone."""

    states = ", ".join(f"{state} {treatment.by_state[state]}" for state in RISK_STATES)
    sources = ", ".join(f"{source} {treatment.by_source.get(source, 0)}" for source in sorted(BY_SOURCE_KEYS))
    reasons = ", ".join(
        f"{reason} {treatment.by_no_state_reason.get(reason, 0)}"
        for reason in NO_STATE_REASONS
    )
    return (
        f"materialized_at {treatment.materialized_at}; pattern_set {treatment.pattern_set}; "
        f"signal_pattern {'on' if treatment.signal_pattern else 'off'}, "
        f"signal_list {'on' if treatment.signal_list else 'off'}; "
        f"clusters {treatment.clusters}; {states}; elements {sources}; "
        f"outside {treatment.outside}; no state {reasons}"
    )
