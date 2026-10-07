"""Write and read corpus cut rows through the worker database role."""

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from gideon.host import report, stack, worker
from gideon.host.corpus.lockfile import Lockfile
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


def _database_fix(rendered_dir: PathLike) -> str:
    return (
        f"Run {stack.logs_fix(rendered_dir, 'postgres')}, then run "
        f"{report.command('corpus cut')} again."
    )


def _tool_fix() -> str:
    return (
        "Restore Docker Compose and psql, then run "
        f"{report.command('corpus cut')} again."
    )


def _run(host: Host, rendered_dir: PathLike, sql: str) -> subprocess.CompletedProcess[str] | Problem:
    try:
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"corpus record command could not run ({type(exc).__name__})",
                       _tool_fix())
    if result.returncode == 127:
        return Problem("corpus record command is unavailable (exit 127)", _tool_fix())
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
    result = _run(host, rendered_dir, sql)
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
        _database_fix(rendered_dir),
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
    result = _run(host, rendered_dir, sql)
    if isinstance(result, Problem):
        return result
    if result.returncode != 0:
        return Problem(f"corpus record read failed (exit {result.returncode})",
                       _database_fix(rendered_dir))
    content = result.stdout.strip()
    if not content:
        return None
    try:
        value: object = json.loads(content)
    except ValueError:
        return Problem("corpus record row is invalid", _database_fix(rendered_dir))
    row = _parse_cut_row(value)
    if row is None or row.label != label:
        return Problem("corpus record row is invalid", _database_fix(rendered_dir))
    return row
