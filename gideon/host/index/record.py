"""Read generation records and write their identities, footprints, and promotions."""

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Literal

from gideon.host import report, staging, worker
from gideon.host.corpus.record import database_fix
from gideon.host.index.generations import GenerationInputs
from gideon.host.index.sample import SOURCE_ID_MAX_LENGTH, SOURCE_ID_PATTERN
from gideon.host.render.worker import (
    GENERATION_FAILURE_REASONS,
    GENERATION_PATTERN,
    GENERATION_STATES,
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
    clustered_at: str | None = None


@dataclass(frozen=True, slots=True)
class PromotedRow:
    """One live generation's identity, collection, and measured bytes."""

    gen_id: str
    collection: str
    bytes: int


@dataclass(frozen=True, slots=True)
class BuildingOutcome:
    """Whether an identity was created or found, with its name and state."""

    outcome: Literal["created", "found"]
    name: str
    state: str


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    """The generations retired by a promotion and its recorded time."""

    retired: tuple[str, ...]
    promoted_at: str


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

_FOOTPRINT_SQL = """WITH written AS (
    UPDATE public.index_generations
    SET counts = counts || jsonb_build_object(
            'lexical_bytes', CASE WHEN counts ? 'lexical_bytes'
                THEN (counts->>'lexical_bytes')::bigint ELSE bytes END,
            'dense_bytes', :'v_dense_bytes'::bigint
        ),
        bytes = CASE WHEN counts ? 'lexical_bytes'
            THEN (counts->>'lexical_bytes')::bigint ELSE bytes END
            + :'v_dense_bytes'::bigint
    WHERE gen_id = :'v_name'
    RETURNING counts, bytes
)
SELECT jsonb_build_object(
    'lexical_bytes', (counts->>'lexical_bytes')::bigint,
    'dense_bytes', (counts->>'dense_bytes')::bigint,
    'bytes', bytes
) FROM written;
"""


_NOT_BUILT = "generation is no longer built"
_PROMOTE_SQL = f"""\\set QUIET on
BEGIN;
SELECT set_config('gideon.promotion_payload', :'v_payload', true) AS stored \\gset
DO $promotion$
DECLARE
    payload jsonb := current_setting('gideon.promotion_payload')::jsonb;
    retired jsonb;
    promoted timestamptz;
BEGIN
    WITH retired_generations AS (
        UPDATE public.index_generations
        SET state = 'retired', retired_at = now()
        WHERE collection = payload->>'collection' AND state = 'live'
          AND gen_id <> payload->>'name'
        RETURNING gen_id
    )
    SELECT COALESCE(jsonb_agg(gen_id ORDER BY gen_id), '[]'::jsonb)
    INTO retired FROM retired_generations;

    UPDATE public.index_generations
    SET state = 'live', promoted_at = now()
    WHERE gen_id = payload->>'name' AND state = 'built'
    RETURNING promoted_at INTO promoted;
    IF NOT FOUND THEN
        RAISE EXCEPTION '{_NOT_BUILT}';
    END IF;
    PERFORM set_config('gideon.promotion_outcome', jsonb_build_object(
        'retired', retired, 'promoted_at', promoted
    )::text, true);
END
$promotion$;
SELECT current_setting('gideon.promotion_outcome')::jsonb;
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
            "promoted_at", "retired_at", "clustered_at",
        ))
    ):
        return None
    if (
        (value["state"] == "failed") != (value["failure_reason"] is not None)
        or value["failure_reason"] is not None
        and value["failure_reason"] not in GENERATION_FAILURE_REASONS
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
        value["clustered_at"],
    )


def _read_row(
    host: Host, rendered_dir: PathLike, sql: str, command_path: str,
    matches: Callable[[GenerationRow], bool],
) -> GenerationRow | None | Problem:
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
    if row is None or not matches(row):
        return Problem("generation record row is invalid", fix)
    return row


def read_generation(
    host: Host, rendered_dir: PathLike, name: str, *, command_path: str = "index build",
) -> GenerationRow | None | Problem:
    """Read one generation, including its half-build timestamps and counts."""

    sql = (
        worker.bind("v_name", name)
        + "\nSELECT row_to_json(generation) FROM public.index_generations AS generation "
        "WHERE gen_id = :'v_name';\n"
    )
    return _read_row(host, rendered_dir, sql, command_path, lambda row: row.gen_id == name)


def read_generation_by_inputs(
    host: Host, rendered_dir: PathLike, collection: str, inputs_hash: str,
    *, command_path: str = "index build",
) -> GenerationRow | None | Problem:
    """Read the generation whose collection and inputs match the requested build."""

    sql = (
        worker.bind("v_collection", collection) + "\n"
        + worker.bind("v_inputs_hash", inputs_hash)
        + "\nSELECT row_to_json(generation) FROM public.index_generations AS generation "
        "WHERE collection = :'v_collection' AND inputs_hash = :'v_inputs_hash';\n"
    )
    return _read_row(
        host, rendered_dir, sql, command_path,
        lambda row: row.collection == collection and row.inputs_hash == inputs_hash,
    )


def read_generations(
    host: Host, rendered_dir: PathLike, *, command_path: str,
) -> tuple[GenerationRow, ...] | Problem:
    """Read every generation in collection, creation time, and id order."""

    sql = (
        "SELECT row_to_json(generation) FROM public.index_generations AS generation "
        "ORDER BY collection, created_at, gen_id;\n"
    )
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"generation record read failed (exit {result.returncode})", fix)
    rows: list[GenerationRow] = []
    for line in result.stdout.splitlines():
        try:
            row = _parse_row(json.loads(line))
        except ValueError:
            row = None
        if row is None:
            return Problem("generation record row is invalid", fix)
        rows.append(row)
    return tuple(rows)


def read_promoted(
    host: Host, rendered_dir: PathLike, *, command_path: str,
) -> tuple[PromotedRow, ...] | Problem:
    """Read the live generations as one array in generation-name order."""

    sql = (
        "SELECT COALESCE(json_agg(promoted ORDER BY gen_id), '[]'::json) FROM (\n"
        "    SELECT gen_id, collection, bytes FROM public.index_generations\n"
        "    WHERE state = 'live'\n"
        ") AS promoted;\n"
    )
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"promoted generation read failed (exit {result.returncode})", fix)
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = None
    if not isinstance(value, list):
        return Problem("promoted generation rows are invalid", fix)
    rows: list[PromotedRow] = []
    for row in value:
        if (
            not isinstance(row, dict) or set(row) != {"gen_id", "collection", "bytes"}
            or not isinstance(row["gen_id"], str) or not row["gen_id"]
            or not isinstance(row["collection"], str) or not row["collection"]
            or type(row["bytes"]) is not int or row["bytes"] < 0
        ):
            return Problem("promoted generation rows are invalid", fix)
        rows.append(PromotedRow(row["gen_id"], row["collection"], row["bytes"]))
    return tuple(rows)


def write_footprint(
    host: Host, rendered_dir: PathLike, name: str, dense_bytes: int, *, command_path: str,
) -> dict[str, int] | Problem:
    """Record the dense footprint and recompute the whole from both halves."""

    sql = "\n".join((
        worker.bind("v_name", name),
        worker.bind("v_dense_bytes", str(dense_bytes)),
        _FOOTPRINT_SQL,
    ))
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"generation footprint write failed (exit {result.returncode})", fix)
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = None
    if (
        not isinstance(value, dict) or set(value) != {"lexical_bytes", "dense_bytes", "bytes"}
        or any(type(count) is not int or count < 0 for count in value.values())
        or value["dense_bytes"] != dense_bytes
        or value["bytes"] != value["lexical_bytes"] + value["dense_bytes"]
    ):
        return Problem("generation footprint write returned invalid byte counts", fix)
    return value


def promote_generation(
    host: Host, rendered_dir: PathLike, name: str, collection: str, *, command_path: str,
) -> PromotionOutcome | Problem:
    """Retire the collection's live rows and promote a built candidate atomically."""

    payload = {"name": name, "collection": collection}
    sql = worker.bind("v_payload", json.dumps(payload, separators=(",", ":"))) + "\n" + _PROMOTE_SQL
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        if _NOT_BUILT in result.stderr:
            return Problem(
                f"{name}: {_NOT_BUILT}; nothing was promoted or retired",
                f"Run {report.command(f'index report {name}')} to read its state, "
                f"then run {report.command(command_path)} again.",
            )
        return Problem(f"generation promotion failed (exit {result.returncode})", fix)
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = None
    if (
        not isinstance(value, dict) or set(value) != {"retired", "promoted_at"}
        or not isinstance(value["retired"], list)
        or any(
            not isinstance(retired, str)
            or re.fullmatch(GENERATION_PATTERN, retired) is None
            or len(retired.encode()) >= 255 or retired == name
            for retired in value["retired"]
        )
        or len(set(value["retired"])) != len(value["retired"])
        or not isinstance(value["promoted_at"], str) or not value["promoted_at"]
    ):
        return Problem("generation promotion returned an invalid outcome", fix)
    return PromotionOutcome(tuple(value["retired"]), value["promoted_at"])


def _parse_manifest_row(line: str, *, with_doc_id: bool) -> dict[str, str] | None:
    """Validate a manifest projection's source id and content-addressed hashes."""

    try:
        value = json.loads(line)
    except ValueError:
        return None
    keys = {"source_id", "canonical_text_sha256"}
    if with_doc_id:
        keys.add("doc_id")
    if (
        not isinstance(value, dict) or set(value) != keys
        or not isinstance(value["source_id"], str)
        or not 1 <= len(value["source_id"]) <= SOURCE_ID_MAX_LENGTH
        or re.fullmatch(SOURCE_ID_PATTERN, value["source_id"]) is None
        or any(
            not isinstance(value[key], str)
            or staging.DIGEST_PATTERN.fullmatch(value[key]) is None
            for key in keys - {"source_id"}
        )
    ):
        return None
    return value


def read_manifest(
    host: Host, rendered_dir: PathLike, name: str, source_ids: Sequence[str], command_path: str,
) -> Mapping[str, str] | Problem:
    """Read recorded hashes for a generation's requested source ids."""

    sql = (
        worker.bind("v_name", name) + "\n"
        + worker.bind("v_source_ids", json.dumps(list(source_ids), separators=(",", ":")))
        + "\nSELECT row_to_json(manifest) FROM ("
        "SELECT source_id, canonical_text_sha256 FROM public.generation_documents "
        "WHERE gen_id = :'v_name' AND source_id IN "
        "(SELECT jsonb_array_elements_text(:'v_source_ids'::jsonb)) "
        "ORDER BY source_id) AS manifest;\n"
    )
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"generation manifest read failed (exit {result.returncode})", fix)
    requested = frozenset(source_ids)
    hashes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        value = _parse_manifest_row(line, with_doc_id=False)
        if (
            value is None
            or value["source_id"] not in requested or value["source_id"] in hashes
        ):
            return Problem("generation manifest row is invalid", fix)
        hashes[value["source_id"]] = value["canonical_text_sha256"]
    return hashes


def read_manifest_rows(
    host: Host, rendered_dir: PathLike, name: str, command_path: str,
) -> tuple[tuple[str, str, str], ...] | Problem:
    """Read every source id, canonical text hash, and document id in source order."""

    sql = (
        worker.bind("v_name", name)
        + "\nSELECT row_to_json(manifest) FROM ("
        "SELECT source_id, canonical_text_sha256, doc_id FROM public.generation_documents "
        "WHERE gen_id = :'v_name' ORDER BY source_id) AS manifest;\n"
    )
    result = _run(host, rendered_dir, sql, command_path)
    if isinstance(result, Problem):
        return result
    fix = database_fix(rendered_dir, command_path)
    if result.returncode != 0:
        return Problem(f"generation manifest read failed (exit {result.returncode})", fix)
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        value = _parse_manifest_row(line, with_doc_id=True)
        if value is None or value["source_id"] in seen:
            return Problem("generation manifest row is invalid", fix)
        seen.add(value["source_id"])
        rows.append((value["source_id"], value["canonical_text_sha256"], value["doc_id"]))
    return tuple(rows)
