"""Write generation identities and read their recorded build progress."""

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Literal

from gideon.host import report, worker
from gideon.host.corpus.record import database_fix
from gideon.host.index.generations import GenerationInputs
from gideon.host.render.worker import (
    GENERATION_PATTERN,
    GENERATION_STATES,
    LEXICAL_FAILURE_REASONS,
)
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike


@dataclass(frozen=True, slots=True)
class GenerationRow:
    """Every column of one generation's identity and lifecycle record."""

    gen_id: str
    collection: str
    corpus_lockfile: str
    lockfiles: tuple[str, ...]
    pipeline: str
    embedding_space: str
    inputs_hash: str
    state: str
    failure_reason: str | None
    counts: Mapping[str, int]
    bytes: int
    lexical_built_at: str | None
    dense_built_at: str | None
    built_at: str | None
    promoted_at: str | None
    retired_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class BuildingOutcome:
    """Whether an identity was created or found, with its name and state."""

    outcome: Literal["created", "found"]
    name: str
    state: str


_WRITE_SQL = """BEGIN;
SELECT set_config('gideon.generation_payload', :'v_payload', true) AS stored \\gset
DO $generation$
DECLARE
    payload jsonb := current_setting('gideon.generation_payload')::jsonb;
    generation public.index_generations%ROWTYPE;
    created boolean;
    label text := payload->>'label';
BEGIN
    INSERT INTO public.index_generations (
        gen_id, collection, corpus_lockfile, lockfiles, pipeline,
        embedding_space, inputs_hash, state
    ) VALUES (
        payload->>'name', payload->>'collection', label, ARRAY[label],
        payload->>'pipeline', payload->>'embedding_space',
        payload->>'inputs_hash', 'building'
    ) ON CONFLICT (collection, inputs_hash) DO NOTHING
    RETURNING * INTO generation;
    created := FOUND;
    IF NOT created THEN
        SELECT * INTO STRICT generation FROM public.index_generations
        WHERE collection = payload->>'collection'
          AND inputs_hash = payload->>'inputs_hash'
        FOR UPDATE;
        IF NOT label = ANY(generation.lockfiles) THEN
            UPDATE public.index_generations
            SET lockfiles = array_append(lockfiles, label)
            WHERE gen_id = generation.gen_id
            RETURNING * INTO generation;
        END IF;
    END IF;
    RAISE NOTICE 'INDEX_GENERATION:%', jsonb_build_object(
        'outcome', CASE WHEN created THEN 'created' ELSE 'found' END,
        'name', generation.gen_id, 'state', generation.state
    );
END
$generation$;
COMMIT;
"""


def _run(
    host: Host, rendered_dir: PathLike, sql: str, command_path: str,
) -> subprocess.CompletedProcess[str] | Problem:
    fix = f"Restore Docker Compose and psql, then run {report.command(command_path)} again."
    try:
        result = host.run(worker.psql_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"generation record command could not run ({type(exc).__name__})", fix)
    if result.returncode == 127:
        return Problem("generation record command is unavailable (exit 127)", fix)
    return result


def write_building(
    host: Host, rendered_dir: PathLike, inputs: GenerationInputs,
    *, command_path: str = "index build",
) -> BuildingOutcome | Problem:
    """Create an identity or append a label to its existing row atomically."""

    payload = {
        "name": inputs.name, "collection": inputs.collection, "label": inputs.label,
        "pipeline": inputs.pipeline, "embedding_space": inputs.embedding_space,
        "inputs_hash": inputs.inputs_hash,
    }
    sql = worker.bind("v_payload", json.dumps(payload, separators=(",", ":"))) + "\n" + _WRITE_SQL
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"generation record write failed (exit {result.returncode})", fix)
    notices = re.findall(r"INDEX_GENERATION:(\{[^\n]*\})", result.stderr)
    try:
        value = json.loads(notices[0]) if len(notices) == 1 else None
    except ValueError:
        value = None
    if (
        not isinstance(value, dict) or set(value) != {"outcome", "name", "state"}
        or value["outcome"] not in ("created", "found")
        or not isinstance(value["name"], str)
        or re.fullmatch(GENERATION_PATTERN, value["name"]) is None
        or len(value["name"].encode()) >= 255
        or value["state"] not in GENERATION_STATES
        or value["outcome"] == "created" and value["name"] != inputs.name
    ):
        return Problem("generation record write returned an invalid outcome", fix)
    return BuildingOutcome(value["outcome"], value["name"], value["state"])


def _parse_row(value: object) -> GenerationRow | None:
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(GenerationRow)}:
        return None
    if (
        not all(isinstance(value[key], str) for key in (
            "gen_id", "collection", "corpus_lockfile", "pipeline", "embedding_space",
            "inputs_hash", "state", "created_at",
        ))
        or value["state"] not in GENERATION_STATES
        or not isinstance(value["lockfiles"], list) or not value["lockfiles"]
        or not all(isinstance(label, str) for label in value["lockfiles"])
        or value["corpus_lockfile"] not in value["lockfiles"]
        or type(value["bytes"]) is not int or value["bytes"] < 0
        or not isinstance(value["counts"], dict)
        or any(not isinstance(key, str) or type(count) is not int or count < 0
               for key, count in value["counts"].items())
        or any(value[key] is not None and not isinstance(value[key], str) for key in (
            "failure_reason", "lexical_built_at", "dense_built_at", "built_at",
            "promoted_at", "retired_at",
        ))
    ):
        return None
    if (
        (value["state"] == "failed") != (value["failure_reason"] is not None)
        or value["failure_reason"] is not None
        and value["failure_reason"] not in LEXICAL_FAILURE_REASONS
        or (value["built_at"] is not None) != (
            value["lexical_built_at"] is not None and value["dense_built_at"] is not None
        )
        or value["promoted_at"] is not None and value["state"] not in {"live", "retired"}
        or (value["state"] == "retired") != (value["retired_at"] is not None)
    ):
        return None
    return GenerationRow(
        value["gen_id"], value["collection"], value["corpus_lockfile"],
        tuple(value["lockfiles"]), value["pipeline"], value["embedding_space"],
        value["inputs_hash"], value["state"], value["failure_reason"], value["counts"],
        value["bytes"], value["lexical_built_at"], value["dense_built_at"],
        value["built_at"], value["promoted_at"], value["retired_at"], value["created_at"],
    )


def read_generation(
    host: Host, rendered_dir: PathLike, name: str, *, command_path: str = "index build",
) -> GenerationRow | None | Problem:
    """Read one generation, including its half-build timestamps and counts."""

    sql = (
        worker.bind("v_name", name)
        + "\nSELECT row_to_json(generation) FROM public.index_generations AS generation "
        "WHERE gen_id = :'v_name';\n"
    )
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"generation record read failed (exit {result.returncode})", fix)
    if not result.stdout.strip():
        return None
    try:
        row = _parse_row(json.loads(result.stdout))
    except ValueError:
        row = None
    if row is None or row.gen_id != name:
        return Problem("generation record row is invalid", fix)
    return row
