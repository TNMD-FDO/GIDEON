"""Write and read corpus cut and upstream observation rows."""

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Final

from gideon.host import report, stack, worker
from gideon.host.corpus.lockfile import LABEL, Lockfile
from gideon.host.render.worker import FAILURE_REASONS, SEGMENT_PATTERN
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


_WRITE_SQL = """BEGIN;
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
END
$cut$;
COMMIT;
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
) FROM public.corpus_lockfiles AS c WHERE c.label = :'v_label';
"""

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


def write_cut(
    host: Host,
    rendered_dir: PathLike,
    lockfile: Lockfile,
    fetched_at: Mapping[str, str],
    verified_at: str,
) -> Problem | None:
    """Write or verify one cut and its snapshots in a single transaction."""

    missing = set(lockfile.sources) - fetched_at.keys()
    if missing:
        return Problem(
            f"fetch times are missing for {', '.join(sorted(missing))}",
            f"Run {report.command('corpus cut')} after all fetch records are present.",
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
    sql = worker.bind("v_payload", json.dumps(payload, separators=(",", ":"))) + "\n" + _WRITE_SQL
    result = _run(host, rendered_dir, sql, "corpus cut")
    if isinstance(result, Problem):
        return result
    if result.returncode == 0:
        return None
    snapshot = re.search(r"CORPUS_SNAPSHOT_CONFLICT:([^:\s]+):([0-9-]+)", result.stderr)
    if snapshot is not None:
        return Problem(
            f"source snapshot {snapshot.group(1)} {snapshot.group(2)} differs from its recorded URL or sidecar digest",
            "Restore the matching lockfile and snapshot, then run "
            f"{report.command('corpus cut')} again.",
        )
    label = re.search(r"CORPUS_LOCKFILE_CONFLICT:([^\s]+)", result.stderr)
    if label is not None:
        return Problem(
            f"lockfile label {label.group(1)} differs from its recorded cut or source bindings",
            "Restore the matching lockfile, then run "
            f"{report.command('corpus cut')} again.",
        )
    return Problem(
        f"corpus record write failed (exit {result.returncode})",
        database_fix(rendered_dir, "corpus cut"),
    )


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
    host: Host, rendered_dir: PathLike, label: str
) -> CutRow | None | Problem:
    """Read one lockfile row and all of its source bindings."""

    sql = worker.bind("v_label", label) + "\n" + _READ_SQL
    result = _run(host, rendered_dir, sql, "corpus cut")
    if isinstance(result, Problem):
        return result
    if result.returncode != 0:
        return Problem(f"corpus record read failed (exit {result.returncode})",
                       database_fix(rendered_dir, "corpus cut"))
    content = result.stdout.strip()
    if not content:
        return None
    try:
        value: object = json.loads(content)
    except ValueError:
        return Problem("corpus record row is invalid", database_fix(rendered_dir, "corpus cut"))
    row = _parse_cut_row(value)
    if row is None or row.label != label:
        return Problem("corpus record row is invalid", database_fix(rendered_dir, "corpus cut"))
    return row


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
