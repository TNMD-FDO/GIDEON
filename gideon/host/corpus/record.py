"""Write and read corpus lockfiles, stage counts, and upstream observations."""

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Final

from gideon.host import report, stack, staging, worker
from gideon.host.corpus.lockfile import LABEL, REASONS, Lockfile
from gideon.host.render.worker import (
    COURT_PATTERN,
    FAILURE_REASONS,
    SEGMENT_PATTERN,
    STAGE_TABLES,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike


@dataclass(frozen=True, slots=True)
class SourceBinding:
    """A lockfile's binding to one dated source snapshot."""

    source: str
    snapshot_date: str


@dataclass(frozen=True, slots=True)
class CutRow:
    """One recorded lockfile row with its source bindings."""

    label: str
    schema: int
    pipeline: str
    cut_at: str
    reason: str
    base: str | None
    installed_at: str | None
    state: str
    sources: tuple[SourceBinding, ...]


@dataclass(frozen=True, slots=True)
class StageCounts:
    """One court's recorded stage counts and the original stage time."""

    dockets: int
    opinion_clusters: int
    citations: int
    opinions: int
    staged_at: str


@dataclass(frozen=True, slots=True)
class InstalledOutcome:
    """An installed label's timestamp and the labels superseded by its move."""

    label: str
    state: str
    installed_at: str
    superseded: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Observation:
    """One source's answered or unanswered index observation."""

    source: str
    observed_at: str
    outcome: str
    latest_label: str | None
    effective_date: str | None
    url: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class WatchState:
    """A source's newest observation, newest answer, and pinned state."""

    newest: Observation
    newest_answered: Observation | None
    first_seen: str | None
    open: bool
    pinned_label: str | None
    pinned_date: str | None
    unanswered_since: str | None


_SQL_START = """BEGIN;
SELECT set_config('gideon.cut_payload', :'v_payload', true) AS stored \\gset
DO $cut$
DECLARE
    payload jsonb := current_setting('gideon.cut_payload')::jsonb;
    pin jsonb;
    cut_label text := payload->>'label';
    label_exists boolean;
    binding_count integer;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM public.corpus_lockfiles WHERE label = cut_label
    ) INTO label_exists;
"""

_SQL_EXISTING_LABEL = """\
    IF label_exists THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.corpus_lockfiles
            WHERE label = cut_label
              AND "schema" = (payload->>'schema')::integer
              AND pipeline = payload->>'pipeline'
              AND cut_at = (payload->>'cut_at')::timestamptz
              AND reason = payload->>'reason'
              AND base IS NOT DISTINCT FROM payload->>'base'
        ) THEN
            RAISE EXCEPTION 'CORPUS_LOCKFILE_CONFLICT:%', cut_label;
        END IF;
        SELECT count(*) INTO binding_count
        FROM public.lockfile_sources WHERE label = cut_label;
        IF binding_count <> jsonb_array_length(payload->'sources') THEN
            RAISE EXCEPTION 'CORPUS_LOCKFILE_CONFLICT:%', cut_label;
        END IF;
        FOR pin IN SELECT value FROM jsonb_array_elements(payload->'sources') LOOP
            IF NOT EXISTS (
                SELECT 1 FROM public.lockfile_sources
                WHERE label = cut_label
                  AND source = pin->>'source'
                  AND snapshot_date = (pin->>'snapshot_date')::date
            ) THEN
                RAISE EXCEPTION 'CORPUS_LOCKFILE_CONFLICT:%', cut_label;
            END IF;
        END LOOP;
    END IF;
"""

_SQL_FULL_SNAPSHOTS = """\
    FOR pin IN SELECT value FROM jsonb_array_elements(payload->'sources') LOOP
        IF EXISTS (
            SELECT 1 FROM public.source_snapshots
            WHERE source = pin->>'source'
              AND snapshot_date = (pin->>'snapshot_date')::date
        ) THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.source_snapshots
                WHERE source = pin->>'source'
                  AND snapshot_date = (pin->>'snapshot_date')::date
                  AND base_url = pin->>'base_url'
                  AND sidecar_sha256 = pin->>'sidecar_sha256'
            ) THEN
                RAISE EXCEPTION 'CORPUS_SNAPSHOT_CONFLICT:%:%',
                    pin->>'source', pin->>'snapshot_date';
            END IF;
            UPDATE public.source_snapshots
            SET mirror_url = pin->>'mirror_url',
                verified_at = (payload->>'verified_at')::timestamptz
            WHERE source = pin->>'source'
              AND snapshot_date = (pin->>'snapshot_date')::date;
        ELSE
            INSERT INTO public.source_snapshots (
                source, snapshot_date, base_url, mirror_url,
                sidecar_sha256, fetched_at, verified_at
            ) VALUES (
                pin->>'source', (pin->>'snapshot_date')::date,
                pin->>'base_url', pin->>'mirror_url', pin->>'sidecar_sha256',
                (pin->>'fetched_at')::timestamptz,
                (payload->>'verified_at')::timestamptz
            );
        END IF;
    END LOOP;
"""

_SQL_INSERT = """\
    IF NOT label_exists THEN
        INSERT INTO public.corpus_lockfiles (
            label, "schema", pipeline, cut_at, reason, base, installed_at, state
        ) VALUES (
            cut_label, (payload->>'schema')::integer, payload->>'pipeline',
            (payload->>'cut_at')::timestamptz, payload->>'reason',
            payload->>'base', NULL, 'cut'
        );
        FOR pin IN SELECT value FROM jsonb_array_elements(payload->'sources') LOOP
            INSERT INTO public.lockfile_sources (label, source, snapshot_date)
            VALUES (cut_label, pin->>'source', (pin->>'snapshot_date')::date);
        END LOOP;
    END IF;
"""

_SQL_INSTALL = """\
    IF COALESCE((payload->>'install')::boolean, false) THEN
        IF EXISTS (
            SELECT 1 FROM public.corpus_lockfiles
            WHERE label = cut_label AND state = 'superseded'
        ) THEN
            RAISE EXCEPTION 'CORPUS_LOCKFILE_SUPERSEDED:%', cut_label;
        END IF;
        UPDATE public.corpus_lockfiles
        SET state = 'installing'
        WHERE label = cut_label AND state = 'cut';
    END IF;
"""

_SQL_END = """\
END
$cut$;
COMMIT;
"""

_SQL_DERIVED_SNAPSHOTS = """\
    FOR pin IN SELECT value FROM jsonb_array_elements(payload->'sources') LOOP
        IF NOT EXISTS (
            SELECT 1 FROM public.source_snapshots
            WHERE source = pin->>'source'
              AND snapshot_date = (pin->>'snapshot_date')::date
        ) THEN
            RAISE EXCEPTION 'CORPUS_SNAPSHOT_MISSING:%:%',
                pin->>'source', pin->>'snapshot_date';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.source_snapshots
            WHERE source = pin->>'source'
              AND snapshot_date = (pin->>'snapshot_date')::date
              AND base_url = pin->>'base_url'
              AND sidecar_sha256 = pin->>'sidecar_sha256'
        ) THEN
            RAISE EXCEPTION 'CORPUS_SNAPSHOT_CONFLICT:%:%',
                pin->>'source', pin->>'snapshot_date';
        END IF;
    END LOOP;
"""

_SQL_DERIVED_BASE = """\
    IF NOT EXISTS (
        SELECT 1 FROM public.corpus_lockfiles WHERE label = payload->>'base'
    ) THEN
        RAISE EXCEPTION 'CORPUS_BASE_MISSING:%', payload->>'base';
    END IF;
"""

_WRITE_SQL = (
    _SQL_START + _SQL_EXISTING_LABEL + _SQL_FULL_SNAPSHOTS
    + _SQL_INSERT + _SQL_INSTALL + _SQL_END
)

_WRITE_INSTALLED_SQL = """BEGIN;
SELECT set_config('gideon.installed_payload', :'v_payload', true) AS stored \\gset
DO $installed$
DECLARE
    payload jsonb := current_setting('gideon.installed_payload')::jsonb;
    cut_label text := payload->>'label';
    installed public.corpus_lockfiles%ROWTYPE;
    superseded jsonb;
BEGIN
    SELECT * INTO installed FROM public.corpus_lockfiles
    WHERE label = cut_label FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'CORPUS_LOCKFILE_MISSING:%', cut_label;
    END IF;
    IF installed.state = 'superseded' THEN
        RAISE EXCEPTION 'CORPUS_LOCKFILE_SUPERSEDED:%', cut_label;
    END IF;
    IF installed.state = 'cut' THEN
        RAISE EXCEPTION 'CORPUS_LOCKFILE_NOT_INSTALLING:%', cut_label;
    END IF;
    IF installed.state = 'installing' THEN
        UPDATE public.corpus_lockfiles
        SET state = 'installed', installed_at = (payload->>'installed_at')::timestamptz
        WHERE label = cut_label
        RETURNING * INTO installed;
    END IF;
    -- Labels sort by their date; a later label is left for its own install.
    WITH moved AS (
        UPDATE public.corpus_lockfiles SET state = 'superseded'
        WHERE label < cut_label AND state IN ('installing', 'installed')
        RETURNING label
    )
    SELECT COALESCE(jsonb_agg(label ORDER BY label), '[]'::jsonb)
    INTO superseded FROM moved;
    RAISE NOTICE 'CORPUS_INSTALLED:%', jsonb_build_object(
        'label', installed.label, 'state', installed.state,
        'installed_at', installed.installed_at, 'superseded', superseded
    );
END
$installed$;
COMMIT;
"""

_WRITE_STAGE_COUNTS_SQL = """\\set QUIET on
BEGIN;
SELECT set_config('gideon.stage_counts_payload', :'v_payload', true) AS stored \\gset
DO $stage_counts$
DECLARE
    payload jsonb := current_setting('gideon.stage_counts_payload')::jsonb;
    counts jsonb;
    recorded public.stage_counts%ROWTYPE;
    inserted integer := 0;
BEGIN
    FOR counts IN SELECT value FROM jsonb_array_elements(payload->'courts') LOOP
        SELECT * INTO recorded FROM public.stage_counts
        WHERE label = payload->>'label' AND source = payload->>'source'
          AND court = counts->>'court';
        IF FOUND THEN
            IF (recorded.dockets, recorded.opinion_clusters, recorded.citations, recorded.opinions)
                IS DISTINCT FROM (
                    (counts->>'dockets')::bigint, (counts->>'opinion_clusters')::bigint,
                    (counts->>'citations')::bigint, (counts->>'opinions')::bigint
                ) THEN
                RAISE EXCEPTION 'STAGE_COUNTS_CONFLICT:%', counts->>'court';
            END IF;
        ELSE
            INSERT INTO public.stage_counts (
                label, source, court, dockets, opinion_clusters, citations, opinions, staged_at
            ) VALUES (
                payload->>'label', payload->>'source', counts->>'court',
                (counts->>'dockets')::bigint, (counts->>'opinion_clusters')::bigint,
                (counts->>'citations')::bigint, (counts->>'opinions')::bigint,
                (payload->>'staged_at')::timestamptz
            );
            inserted := inserted + 1;
        END IF;
    END LOOP;
    PERFORM set_config('gideon.stage_counts_inserted', inserted::text, true);
END
$stage_counts$;
SELECT current_setting('gideon.stage_counts_inserted') AS v_inserted \\gset
COMMIT;
SELECT json_build_object('inserted', :'v_inserted'::integer);
"""

_READ_STAGE_COUNTS_SQL = """SELECT json_agg(row_to_json(s) ORDER BY source, court)
FROM public.stage_counts s WHERE label = :'v_label';
"""

_READ_SQL = """SELECT jsonb_build_object(
    'label', c.label,
    'schema', c."schema",
    'pipeline', c.pipeline,
    'cut_at', c.cut_at,
    'reason', c.reason,
    'base', c.base,
    'installed_at', c.installed_at,
    'state', c.state,
    'sources', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'source', binding.source,
            'snapshot_date', binding.snapshot_date
        ) ORDER BY binding.source)
        FROM public.lockfile_sources AS binding WHERE binding.label = c.label
    ), '[]'::jsonb)
) FROM public.corpus_lockfiles AS c"""

_READ_CUT_SQL = _READ_SQL + " WHERE c.label = :'v_label';\n"
_READ_LOCKFILES_SQL = _READ_SQL + " ORDER BY c.label;\n"

_READ_COPY_SQL = """WITH RECURSIVE chain AS (
    SELECT label, base, 0 AS depth, ARRAY[label] AS path
    FROM public.corpus_lockfiles WHERE label = :'v_label'
    UNION ALL
    SELECT c.label, c.base, chain.depth + 1, chain.path || c.label
    FROM public.corpus_lockfiles AS c
    JOIN chain ON c.label = chain.base
    WHERE NOT c.label = ANY(chain.path)
)
SELECT to_jsonb(c) || jsonb_build_object(
    'sources', COALESCE((
        SELECT jsonb_agg(to_jsonb(snapshot) ORDER BY binding.source)
        FROM public.lockfile_sources AS binding
        JOIN public.source_snapshots AS snapshot
          ON snapshot.source = binding.source
         AND snapshot.snapshot_date = binding.snapshot_date
        WHERE binding.label = c.label
    ), '[]'::jsonb)
)
FROM chain JOIN public.corpus_lockfiles AS c ON c.label = chain.label
ORDER BY chain.depth DESC;
"""

_SQL_COPY_SNAPSHOTS = """\
    FOR pin IN SELECT value FROM jsonb_array_elements(payload->'sources') LOOP
        INSERT INTO public.source_snapshots (
            source, snapshot_date, base_url, mirror_url,
            sidecar_sha256, fetched_at, verified_at
        ) VALUES (
            pin->>'source', (pin->>'snapshot_date')::date,
            pin->>'base_url', pin->>'mirror_url', pin->>'sidecar_sha256',
            (pin->>'fetched_at')::timestamptz, (pin->>'verified_at')::timestamptz
        ) ON CONFLICT (source, snapshot_date) DO NOTHING;
    END LOOP;
"""

_SQL_COPY_INSERT = """\
    IF NOT label_exists THEN
        INSERT INTO public.corpus_lockfiles (
            label, "schema", pipeline, cut_at, reason, base, installed_at, state
        ) VALUES (
            cut_label, (payload->>'schema')::integer, payload->>'pipeline',
            (payload->>'cut_at')::timestamptz, payload->>'reason', payload->>'base',
            (payload->>'installed_at')::timestamptz, payload->>'state'
        );
        FOR pin IN SELECT value FROM jsonb_array_elements(payload->'sources') LOOP
            INSERT INTO public.lockfile_sources (label, source, snapshot_date)
            VALUES (cut_label, pin->>'source', (pin->>'snapshot_date')::date);
        END LOOP;
    END IF;
"""

# A copied label's state is the source's at the copy: it moves there afterwards
# (installing, installed, superseded), so the existing-label check compares the
# immutable columns alone and the copy's reader judges state on the source.
_COPY_WRITE_SQL = (
    _SQL_START + _SQL_EXISTING_LABEL + _SQL_COPY_SNAPSHOTS
    + _SQL_DERIVED_SNAPSHOTS + _SQL_COPY_INSERT + _SQL_END
)

_WRITE_OBSERVATIONS_SQL = """BEGIN;
SELECT set_config('gideon.observations_payload', :'v_payload', true) AS stored \\gset
INSERT INTO public.upstream_observations (
    source, observed_at, outcome, latest_label, effective_date, url, detail
)
SELECT
    item->>'source', (item->>'observed_at')::timestamptz,
    item->>'outcome', (item->>'latest_label')::date,
    (item->>'effective_date')::date, item->>'url', item->>'detail'
FROM jsonb_array_elements(current_setting('gideon.observations_payload')::jsonb) AS item;
COMMIT;
"""

# One relational definition is used by the command and the proposals section.
# The Grafana rule keeps a tested copy because rendering cannot import this module.
WATCH_STATE_SQL = """WITH newest AS (
    SELECT DISTINCT ON (source) *
    FROM public.upstream_observations
    ORDER BY source, observed_at DESC
), answered AS (
    SELECT DISTINCT ON (source) *
    FROM public.upstream_observations
    WHERE outcome = 'observed'
    ORDER BY source, observed_at DESC
)
SELECT
    n.source,
    n.observed_at AS newest_at,
    n.outcome AS newest_outcome,
    n.latest_label AS newest_latest_label,
    n.effective_date AS newest_effective_date,
    n.url AS newest_url,
    n.detail AS newest_detail,
    a.observed_at AS answered_at,
    a.outcome AS answered_outcome,
    a.latest_label AS answered_latest_label,
    a.effective_date AS answered_effective_date,
    a.url AS answered_url,
    a.detail AS answered_detail,
    first_seen.observed_at AS first_seen,
    a.latest_label IS NOT NULL
        AND (binding.snapshot_date IS NULL OR binding.snapshot_date < a.latest_label) AS open,
    binding.label AS pinned_label,
    binding.snapshot_date AS pinned_date,
    CASE WHEN n.outcome = 'unanswered' THEN (
        SELECT MIN(u.observed_at)
        FROM public.upstream_observations AS u
        WHERE u.source = n.source AND u.outcome = 'unanswered'
          AND (a.observed_at IS NULL OR u.observed_at > a.observed_at)
    ) END AS unanswered_since
FROM newest AS n
LEFT JOIN answered AS a ON a.source = n.source
LEFT JOIN LATERAL (
    SELECT MIN(seen.observed_at) AS observed_at
    FROM public.upstream_observations AS seen
    WHERE seen.source = a.source AND seen.outcome = 'observed'
      AND seen.latest_label = a.latest_label
) AS first_seen ON true
LEFT JOIN LATERAL (
    SELECT b.label, b.snapshot_date
    FROM public.lockfile_sources AS b
    JOIN public.corpus_lockfiles AS c ON c.label = b.label
    WHERE b.source = n.source
    ORDER BY b.snapshot_date DESC, c.cut_at DESC, b.label DESC
    LIMIT 1
) AS binding ON true
ORDER BY n.source"""
WATCH_STATE_STATEMENT: Final = f"SELECT row_to_json(state) FROM ({WATCH_STATE_SQL}) AS state;\n"
# An unanswered row's reasons: the fetch's own, the resolve bound's and reader's,
# and the worker's absence, which the watch records without a resolve.
UNANSWERED_REASONS: Final = FAILURE_REASONS | {
    "timeout", "missing-index", "unreadable-index", "worker-unavailable",
}
_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def database_fix(rendered_dir: PathLike, command_path: str) -> str:
    """Name the database's logs and the calling command's re-run."""
    return (
        f"Run {stack.logs_fix(rendered_dir, 'postgres')}, then run "
        f"{report.command(command_path)} again."
    )


def _tool_fix(command_path: str) -> str:
    return (
        "Restore Docker Compose and psql, then run "
        f"{report.command(command_path)} again."
    )


def _run(
    host: Host, rendered_dir: PathLike, sql: str, command_path: str
) -> subprocess.CompletedProcess[str] | Problem:
    try:
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"corpus record command could not run ({type(exc).__name__})",
                       _tool_fix(command_path))
    if result.returncode == 127:
        return Problem("corpus record command is unavailable (exit 127)",
                       _tool_fix(command_path))
    return result


def _write_lockfile(
    host: Host,
    rendered_dir: PathLike,
    lockfile: Lockfile,
    fetched_at: Mapping[str, str],
    verified_at: str,
    *,
    install: bool,
    command_path: str,
) -> Problem | None:
    missing = set(lockfile.sources) - fetched_at.keys()
    if missing:
        return Problem(
            f"fetch times are missing for {', '.join(sorted(missing))}",
            f"Run {report.command(command_path)} after all fetch records are present.",
        )
    payload = {
        "label": lockfile.label,
        "schema": lockfile.schema,
        "pipeline": lockfile.pipeline,
        "cut_at": lockfile.cut_at,
        "reason": lockfile.reason,
        "base": lockfile.base,
        "verified_at": verified_at,
        "sources": [
            {
                "source": name,
                "snapshot_date": pin.snapshot_date,
                "base_url": pin.base_url,
                "mirror_url": pin.mirror_url,
                "sidecar_sha256": pin.sidecar_sha256,
                "fetched_at": fetched_at[name],
            }
            for name, pin in lockfile.sources.items()
        ],
    }
    if install:
        payload["install"] = True
    sql = worker.bind("v_payload", json.dumps(payload, separators=(",", ":"))) + "\n" + _WRITE_SQL
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    if result.returncode == 0:
        return None
    return _write_failure(
        result, rendered_dir, command_path=command_path,
        snapshot_subject=f"lockfile label {lockfile.label}: " if install else "",
    )


def _write_failure(
    result: subprocess.CompletedProcess[str],
    rendered_dir: PathLike,
    *,
    command_path: str,
    snapshot_subject: str = "",
    install_label: str | None = None,
) -> Problem:
    if install_label is not None:
        snapshot_missing = re.search(r"CORPUS_SNAPSHOT_MISSING:([^:\s]+):([0-9-]+)", result.stderr)
        if snapshot_missing is not None:
            return Problem(
                f"source snapshot {snapshot_missing.group(1)} {snapshot_missing.group(2)} "
                "is not recorded",
                f"Run {report.command('corpus install')} {install_label}, then run "
                f"{report.command(command_path)} again.",
            )
        base_missing = re.search(r"CORPUS_BASE_MISSING:([^\s]+)", result.stderr)
        if base_missing is not None:
            return Problem(
                f"base {base_missing.group(1)} is not recorded",
                f"Run {report.command('corpus install')} {base_missing.group(1)}, then run "
                f"{report.command(command_path)} again.",
            )
    superseded = re.search(r"CORPUS_LOCKFILE_SUPERSEDED:([^\s]+)", result.stderr)
    if superseded is not None:
        return Problem(
            f"lockfile label {superseded.group(1)} is superseded",
            f"Run {report.command(command_path)} with the newest committed label.",
        )
    missing = re.search(r"CORPUS_LOCKFILE_MISSING:([^\s]+)", result.stderr)
    if missing is not None:
        return Problem(
            f"lockfile label {missing.group(1)} is not recorded",
            f"Run {report.command(command_path)} again.",
        )
    not_installing = re.search(r"CORPUS_LOCKFILE_NOT_INSTALLING:([^\s]+)", result.stderr)
    if not_installing is not None:
        return Problem(
            f"lockfile label {not_installing.group(1)} is not installing",
            f"Run {report.command(command_path)} again.",
        )
    snapshot = re.search(r"CORPUS_SNAPSHOT_CONFLICT:([^:\s]+):([0-9-]+)", result.stderr)
    if snapshot is not None:
        return Problem(
            f"{snapshot_subject}source snapshot {snapshot.group(1)} {snapshot.group(2)} "
            "differs from its recorded URL or sidecar digest",
            "Restore the matching lockfile and snapshot, then run "
            f"{report.command(command_path)} again.",
        )
    label = re.search(r"CORPUS_LOCKFILE_CONFLICT:([^\s]+)", result.stderr)
    if label is not None:
        return Problem(
            f"lockfile label {label.group(1)} differs from its recorded cut or source bindings",
            "Restore the matching lockfile, then run "
            f"{report.command(command_path)} again.",
        )
    return Problem(
        f"corpus record write failed (exit {result.returncode})",
        database_fix(rendered_dir, command_path),
    )


def write_cut(
    host: Host,
    rendered_dir: PathLike,
    lockfile: Lockfile,
    fetched_at: Mapping[str, str],
    verified_at: str,
    *,
    command_path: str,
) -> Problem | None:
    """Write or verify one cut and its snapshots in a single transaction."""
    return _write_lockfile(
        host, rendered_dir, lockfile, fetched_at, verified_at,
        install=False, command_path=command_path,
    )


def write_install(
    host: Host,
    rendered_dir: PathLike,
    lockfile: Lockfile,
    fetched_at: Mapping[str, str],
    verified_at: str,
    *,
    command_path: str,
) -> Problem | None:
    """Write an installing label or keep its installed state after comparison."""
    return _write_lockfile(
        host, rendered_dir, lockfile, fetched_at, verified_at,
        install=True, command_path=command_path,
    )


def write_installed(
    host: Host, rendered_dir: PathLike, label: str, installed_at: str,
    *, command_path: str,
) -> InstalledOutcome | Problem:
    """Install a label and supersede other active labels in one transaction."""

    payload = {"label": label, "installed_at": installed_at}
    sql = (
        worker.bind("v_payload", json.dumps(payload, separators=(",", ":")))
        + "\n" + _WRITE_INSTALLED_SQL
    )
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    if result.returncode != 0:
        return _write_failure(result, rendered_dir, command_path=command_path)
    notices = re.findall(r"CORPUS_INSTALLED:(\{[^\n]*\})", result.stderr)
    try:
        value = json.loads(notices[0]) if len(notices) == 1 else None
    except ValueError:
        value = None
    if (
        not isinstance(value, dict)
        or set(value) != {"label", "state", "installed_at", "superseded"}
        or value["label"] != label or value["state"] != "installed"
        or not _is_timestamp(value["installed_at"])
        or not isinstance(value["superseded"], list)
        or any(not isinstance(item, str) or LABEL.fullmatch(item) is None or item == label
               for item in value["superseded"])
        or value["superseded"] != sorted(set(value["superseded"]))
    ):
        return Problem("corpus record write returned an invalid outcome",
                       database_fix(rendered_dir, command_path))
    return InstalledOutcome(
        value["label"], value["state"], value["installed_at"], tuple(value["superseded"]),
    )


def write_derived_cut(
    host: Host, rendered_dir: PathLike, lockfile: Lockfile, *, command_path: str,
) -> Problem | None:
    """Record a derived lockfile against existing, unchanged snapshot rows."""
    payload = {
        "label": lockfile.label,
        "schema": lockfile.schema,
        "pipeline": lockfile.pipeline,
        "cut_at": lockfile.cut_at,
        "reason": lockfile.reason,
        "base": lockfile.base,
        "sources": [
            {
                "source": name,
                "snapshot_date": pin.snapshot_date,
                "base_url": pin.base_url,
                "mirror_url": pin.mirror_url,
                "sidecar_sha256": pin.sidecar_sha256,
            }
            for name, pin in lockfile.sources.items()
        ],
    }
    statement = (
        _SQL_START + _SQL_EXISTING_LABEL + _SQL_DERIVED_SNAPSHOTS
        + (_SQL_DERIVED_BASE if lockfile.base is not None else "")
        + _SQL_INSERT + _SQL_END
    )
    sql = worker.bind("v_payload", json.dumps(payload, separators=(",", ":"))) + "\n" + statement
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    if result.returncode == 0:
        return None
    return _write_failure(
        result, rendered_dir, command_path=command_path,
        install_label=lockfile.base or lockfile.label,
    )


def write_stage_counts(
    host: Host, rendered_dir: PathLike, label: str, source: str, record: staging.StageRecord,
    *, command_path: str,
) -> int | Problem:
    """Insert a validated stage's counts or compare the existing counts unchanged."""
    payload = {
        "label": label,
        "source": source,
        "staged_at": record.staged_at,
        "courts": [
            {
                "court": court,
                **{table.replace("-", "_"): record.counts[court][table]
                   for table in STAGE_TABLES[1:]},
            }
            for court in record.courts
        ],
    }
    sql = worker.bind("v_payload", json.dumps(payload, separators=(",", ":"))) + "\n" + _WRITE_STAGE_COUNTS_SQL
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    if result.returncode != 0:
        conflict = re.search(r"STAGE_COUNTS_CONFLICT:([^\s]+)", result.stderr)
        if conflict is not None:
            return Problem(
                f"stage counts for {conflict.group(1)} differ from the recorded ones",
                "A label's counts are recorded once: make a new lockfile with "
                f"{report.command('corpus cut')}, then install it with "
                f"{report.command('corpus install')}.",
            )
        return _write_failure(result, rendered_dir, command_path=command_path)
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = None
    if (
        not isinstance(value, dict) or set(value) != {"inserted"}
        or not isinstance(value["inserted"], int) or isinstance(value["inserted"], bool)
        or not 0 <= value["inserted"] <= len(record.courts)
    ):
        return Problem("stage counts write returned an invalid outcome",
                       database_fix(rendered_dir, command_path))
    return value["inserted"]


def read_stage_counts(
    host: Host, directory: PathLike, label: str, *, command_path: str,
) -> dict[str, dict[str, StageCounts]] | Problem:
    """Read a label's stage counts through the caller's corpus database directory."""
    sql = worker.bind("v_label", label) + "\n" + _READ_STAGE_COUNTS_SQL
    result = _run(host, directory, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(directory, command_path)
    if result.returncode != 0:
        return Problem(f"stage counts read failed (exit {result.returncode})", fix)
    content = result.stdout.strip()
    if not content:
        return {}
    try:
        value = json.loads(content)
    except ValueError:
        return Problem("stage counts row is invalid", fix)
    if value is None:
        return {}
    if not isinstance(value, list):
        return Problem("stage counts row is invalid", fix)
    rows: dict[str, dict[str, StageCounts]] = {}
    for row in value:
        if (
            not isinstance(row, dict) or set(row) != {
                "label", "source", "court", "dockets", "opinion_clusters",
                "citations", "opinions", "staged_at",
            }
            or row["label"] != label
            or not isinstance(row["source"], str) or re.fullmatch(SEGMENT_PATTERN, row["source"]) is None
            or not isinstance(row["court"], str) or re.fullmatch(COURT_PATTERN, row["court"]) is None
            or any(not isinstance(row[key], int) or isinstance(row[key], bool) or row[key] < 0
                   for key in ("dockets", "opinion_clusters", "citations", "opinions"))
            or not _is_timestamp(row["staged_at"])
            or row["court"] in rows.get(row["source"], {})
        ):
            return Problem("stage counts row is invalid", fix)
        rows.setdefault(row["source"], {})[row["court"]] = StageCounts(
            row["dockets"], row["opinion_clusters"], row["citations"], row["opinions"], row["staged_at"],
        )
    return rows


def _parse_cut_row(value: object) -> CutRow | None:
    if not isinstance(value, dict) or set(value) != {
        "label", "schema", "pipeline", "cut_at", "reason", "base",
        "installed_at", "state", "sources",
    }:
        return None
    if (
        not isinstance(value["label"], str)
        or not isinstance(value["schema"], int) or isinstance(value["schema"], bool)
        or not all(isinstance(value[key], str) for key in ("pipeline", "cut_at", "reason", "state"))
        or (value["base"] is not None and not isinstance(value["base"], str))
        or (value["installed_at"] is not None and not isinstance(value["installed_at"], str))
        or not isinstance(value["sources"], list)
    ):
        return None
    bindings: list[SourceBinding] = []
    for source in value["sources"]:
        if (
            not isinstance(source, dict)
            or set(source) != {"source", "snapshot_date"}
            or not isinstance(source["source"], str)
            or not isinstance(source["snapshot_date"], str)
        ):
            return None
        bindings.append(SourceBinding(source["source"], source["snapshot_date"]))
    return CutRow(
        value["label"], value["schema"], value["pipeline"], value["cut_at"],
        value["reason"], value["base"], value["installed_at"], value["state"],
        tuple(bindings),
    )


def read_cut(
    host: Host, rendered_dir: PathLike, label: str, *, command_path: str,
) -> CutRow | None | Problem:
    """Read one lockfile row and all of its source bindings."""

    sql = worker.bind("v_label", label) + "\n" + _READ_CUT_SQL
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    if result.returncode != 0:
        return Problem(f"corpus record read failed (exit {result.returncode})",
                       database_fix(rendered_dir, command_path))
    content = result.stdout.strip()
    if not content:
        return None
    try:
        value: object = json.loads(content)
    except ValueError:
        return Problem("corpus record row is invalid",
                       database_fix(rendered_dir, command_path))
    row = _parse_cut_row(value)
    if row is None or row.label != label:
        return Problem("corpus record row is invalid",
                       database_fix(rendered_dir, command_path))
    return row


def read_lockfiles(
    host: Host, rendered_dir: PathLike, *, command_path: str,
) -> tuple[CutRow, ...] | Problem:
    """Read every recorded lockfile and its bindings in label order."""
    result = _run(host, rendered_dir, _READ_LOCKFILES_SQL, command_path)
    if isinstance(result, Problem):
        return result
    if result.returncode != 0:
        return Problem(f"corpus record read failed (exit {result.returncode})",
                       database_fix(rendered_dir, command_path))
    rows: list[CutRow] = []
    for line in result.stdout.splitlines():
        try:
            value: object = json.loads(line)
        except ValueError:
            return Problem("corpus record row is invalid",
                           database_fix(rendered_dir, command_path))
        row = _parse_cut_row(value)
        if row is None:
            return Problem("corpus record row is invalid",
                           database_fix(rendered_dir, command_path))
        rows.append(row)
    return tuple(rows)


def _parse_copy_row(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        return None
    sources: list[dict[str, str]] = []
    for pin in value["sources"]:
        if (
            not isinstance(pin, dict) or set(pin) != {
                "source", "snapshot_date", "base_url", "mirror_url",
                "sidecar_sha256", "fetched_at", "verified_at",
            }
            or any(not isinstance(pin[key], str) for key in (
                "source", "snapshot_date", "base_url", "sidecar_sha256", "fetched_at", "verified_at",
            ))
            or pin["mirror_url"] is not None and not isinstance(pin["mirror_url"], str)
            or re.fullmatch(SEGMENT_PATTERN, pin["source"]) is None
            or not _is_date(pin["snapshot_date"])
            or re.fullmatch(r"[0-9a-f]{64}", pin["sidecar_sha256"]) is None
            or not _is_timestamp(pin["fetched_at"]) or not _is_timestamp(pin["verified_at"])
        ):
            return None
        sources.append({"source": pin["source"], "snapshot_date": pin["snapshot_date"]})
    row = _parse_cut_row({**value, "sources": sources})
    if (
        row is None or LABEL.fullmatch(row.label) is None or row.schema < 1
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", row.pipeline) is None
        or row.reason not in REASONS or row.state not in {"cut", "installing", "installed", "superseded"}
        or not _is_timestamp(row.cut_at)
        or row.installed_at is not None and not _is_timestamp(row.installed_at)
        or len({pin.source for pin in row.sources}) != len(row.sources)
    ):
        return None
    return value


def copy_lockfile(
    host: Host, source_dir: PathLike, target_dir: PathLike, label: str, command_path: str,
    *, conflict_fix: str,
) -> Problem | None:
    """Copy a recorded lockfile and its base chain without changing existing rows.

    A target row that differs from the source's is the target's divergence, so
    its refusal carries the caller's ``conflict_fix``.
    """

    sql = worker.bind("v_label", label) + "\n" + _READ_COPY_SQL
    result = _run(host, source_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(source_dir, command_path)
    if result.returncode != 0:
        return Problem(f"corpus record read failed (exit {result.returncode})", fix)
    if not result.stdout.strip():
        return Problem(
            f"lockfile label {label} is not recorded",
            f"Run {report.command('corpus install')} {label}, then run {report.command(command_path)} again.",
        )
    rows: list[Mapping[str, object]] = []
    labels: set[str] = set()
    base: str | None = None
    for line in result.stdout.splitlines():
        try:
            row = _parse_copy_row(json.loads(line))
        except ValueError:
            row = None
        if row is None or row["base"] != base or row["label"] in labels:
            return Problem("corpus record row is invalid", fix)
        assert isinstance(row["label"], str)
        base = row["label"]
        labels.add(base)
        rows.append(row)
    if base != label:
        return Problem("corpus record row is invalid", fix)
    for row in rows:
        sql = worker.bind("v_payload", json.dumps(row, separators=(",", ":"))) + "\n" + _COPY_WRITE_SQL
        result = _run(host, target_dir, sql, command_path)
        if isinstance(result, Problem):
            return result
        if result.returncode != 0:
            problem = _write_failure(result, target_dir, command_path=command_path)
            if re.search(r"CORPUS_(LOCKFILE|SNAPSHOT)_CONFLICT:", result.stderr) is not None:
                return Problem(problem.problem, conflict_fix)
            return problem
    return None


def write_observations(
    host: Host,
    rendered_dir: PathLike,
    observations: Sequence[Observation],
    *,
    command_path: str = "corpus watch",
) -> Problem | None:
    """Insert one run's observations in a single transaction."""

    payload = json.dumps([asdict(item) for item in observations], separators=(",", ":"))
    sql = worker.bind("v_payload", payload) + "\n" + _WRITE_OBSERVATIONS_SQL
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    if result.returncode != 0:
        return Problem(
            f"upstream observations write failed (exit {result.returncode})",
            database_fix(rendered_dir, command_path),
        )
    return None


_OBSERVATION_FIELDS = ("at", "outcome", "latest_label", "effective_date", "url", "detail")
_WATCH_STATE_FIELDS = {
    "source", "first_seen", "open", "pinned_label", "pinned_date", "unanswered_since",
    *(f"{prefix}_{field}" for prefix in ("newest", "answered") for field in _OBSERVATION_FIELDS),
}


def _is_date(value: object) -> bool:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _parse_observation(value: dict[str, object], prefix: str, source: str) -> Observation | None:
    at = value[f"{prefix}_at"]
    outcome = value[f"{prefix}_outcome"]
    label = value[f"{prefix}_latest_label"]
    effective_date = value[f"{prefix}_effective_date"]
    url = value[f"{prefix}_url"]
    detail = value[f"{prefix}_detail"]
    if (
        not isinstance(at, str) or not _is_timestamp(at)
        or not isinstance(outcome, str) or outcome not in {"observed", "unanswered"}
        or (label is not None and not _is_date(label))
        or (effective_date is not None and not _is_date(effective_date))
        or not isinstance(url, str) or not url.startswith("https://")
        or (detail is not None and detail not in UNANSWERED_REASONS)
        or (outcome == "observed" and (label is None or detail is not None))
        or (outcome == "unanswered" and (label is not None or detail is None))
    ):
        return None
    assert label is None or isinstance(label, str)
    assert effective_date is None or isinstance(effective_date, str)
    assert detail is None or isinstance(detail, str)
    return Observation(source, at, outcome, label, effective_date, url, detail)


def parse_watch_state(line: str) -> WatchState | None:
    """Parse one JSON line of the watch-state statement, or none when unreadable."""

    try:
        value: object = json.loads(line)
    except ValueError:
        return None
    if not isinstance(value, dict) or set(value) != _WATCH_STATE_FIELDS:
        return None
    source = value["source"]
    if not isinstance(source, str) or re.fullmatch(SEGMENT_PATTERN, source) is None:
        return None
    newest = _parse_observation(value, "newest", source)
    if newest is None:
        return None
    answered_at = value["answered_at"]
    if answered_at is None:
        if any(value[f"answered_{field}"] is not None for field in _OBSERVATION_FIELDS[1:]):
            return None
        answered = None
    else:
        answered = _parse_observation(value, "answered", source)
        if answered is None or answered.outcome != "observed":
            return None
    if newest.outcome == "observed" and answered is None:
        return None
    first_seen = value["first_seen"]
    open_notice = value["open"]
    pinned_label = value["pinned_label"]
    pinned_date = value["pinned_date"]
    unanswered_since = value["unanswered_since"]
    if (
        (first_seen is None) != (answered is None)
        or (first_seen is not None and not _is_timestamp(first_seen))
        or not isinstance(open_notice, bool)
        or (answered is None and open_notice)
        or (pinned_label is None) != (pinned_date is None)
        or (pinned_label is not None
            and (not isinstance(pinned_label, str) or LABEL.fullmatch(pinned_label) is None))
        or (pinned_date is not None and not _is_date(pinned_date))
        or (unanswered_since is None) != (newest.outcome == "observed")
        or (unanswered_since is not None and not _is_timestamp(unanswered_since))
    ):
        return None
    assert first_seen is None or isinstance(first_seen, str)
    assert pinned_date is None or isinstance(pinned_date, str)
    assert unanswered_since is None or isinstance(unanswered_since, str)
    return WatchState(
        newest, answered, first_seen, open_notice,
        pinned_label, pinned_date, unanswered_since,
    )


def read_watch_state(
    host: Host,
    rendered_dir: PathLike,
    *,
    command_path: str = "corpus watch",
) -> tuple[WatchState, ...] | Problem:
    """Read the newest and newest answered observations for every source."""

    result = _run(host, rendered_dir, WATCH_STATE_STATEMENT, command_path)
    if isinstance(result, Problem):
        return result
    if result.returncode != 0:
        return Problem(
            f"upstream observations read failed (exit {result.returncode})",
            database_fix(rendered_dir, command_path),
        )
    states: list[WatchState] = []
    for line in result.stdout.splitlines():
        state = parse_watch_state(line)
        if state is None:
            return Problem("upstream observation row is invalid", database_fix(rendered_dir, command_path))
        states.append(state)
    return tuple(states)
