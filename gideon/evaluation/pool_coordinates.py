"""Resolve ranked pool chunks through the corpus record and generation manifest."""

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Final, TypeGuard

from gideon.evaluation import record
from gideon.evaluation.rankmetrics import Coordinates
from gideon.evaluation.results import RunContext
from gideon.host import stack
from gideon.host.index.sample import SOURCE_ID_MAX_LENGTH, SOURCE_ID_PATTERN
from gideon.host.render.worker import GENERATION_PATTERN
from gideon.host.report import Problem
from gideon.host.staging import DIGEST_PATTERN
from gideon.host.sysio import Host, PathLike
from gideon.host.worker import bind

# exempt: a bound on the number of identifiers in one psql variable.
BATCH_SIZE: Final = 500
_CHUNKS_SQL: Final = """SELECT row_to_json(chunk) FROM (
    SELECT chunk_id, doc_id, char_start, char_end FROM public.chunks
    WHERE chunk_id IN (SELECT jsonb_array_elements_text(:'v_ids'::jsonb))
    ORDER BY chunk_id
) AS chunk;"""
_MANIFEST_SQL: Final = """SELECT row_to_json(manifest) FROM (
    SELECT doc_id, source_id, canonical_text_sha256 FROM public.generation_documents
    WHERE gen_id = :'v_name' AND doc_id IN (SELECT jsonb_array_elements_text(:'v_ids'::jsonb))
    ORDER BY doc_id, source_id
) AS manifest;"""


def _digest(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and DIGEST_PATTERN.fullmatch(value) is not None


def _chunk_row(row: dict[str, object]) -> tuple[str, tuple[str, int, int]] | None:
    chunk_id, doc_id = row.get("chunk_id"), row.get("doc_id")
    start, end = row.get("char_start"), row.get("char_end")
    if (
        row.keys() != {"chunk_id", "doc_id", "char_start", "char_end"}
        or not _digest(chunk_id) or not _digest(doc_id)
        or type(start) is not int or type(end) is not int or not 0 <= start < end
    ):
        return None
    return chunk_id, (doc_id, start, end)


def _manifest_row(row: dict[str, object]) -> tuple[str, tuple[str, str]] | None:
    doc_id, source_id, digest = row.get("doc_id"), row.get("source_id"), row.get("canonical_text_sha256")
    if (
        row.keys() != {"doc_id", "source_id", "canonical_text_sha256"}
        or not _digest(doc_id) or not _digest(digest)
        or not isinstance(source_id, str) or not 1 <= len(source_id) <= SOURCE_ID_MAX_LENGTH
        or re.fullmatch(SOURCE_ID_PATTERN, source_id) is None
    ):
        return None
    return doc_id, (source_id, digest)


def _read_rows[T](
    host: Host, rendered_dir: PathLike, ids: set[str], *,
    label: str, statement: str, parse: Callable[[dict[str, object]], tuple[str, T] | None],
    header: str = "",
) -> dict[str, T] | Problem:
    """Read bounded batches in one psql invocation and validate rows across them."""

    if not ids:
        return {}
    ordered = sorted(ids)
    lines = [header] if header else []
    for start in range(0, len(ordered), BATCH_SIZE):
        lines.append(bind("v_ids", json.dumps(ordered[start:start + BATCH_SIZE], separators=(",", ":"))))
        lines.append(statement)

    fix = f"Run {stack.logs_fix(rendered_dir, record.POSTGRES_SERVICE)}, then retry."
    try:
        result = host.run(record.reader_argv(rendered_dir), input="\n".join(lines) + "\n")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return Problem(f"{label} read could not run", fix)
    if result.returncode != 0:
        return Problem(f"{label} read failed (exit {result.returncode})", fix)

    rows: dict[str, T] = {}
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            return Problem(f"{label} row is invalid", fix)
        parsed = parse(value) if isinstance(value, dict) else None
        if parsed is None:
            return Problem(f"{label} row is invalid", fix)
        identifier, fields = parsed
        if identifier not in ids or identifier in rows:
            return Problem(f"{label} row is invalid", fix)
        rows[identifier] = fields
    return rows


def read_coordinates(
    name: str, pools: Mapping[str, Sequence[str]], context: RunContext,
) -> dict[str, tuple[Coordinates | None, ...]] | Problem:
    """Resolve every distinct chunk once, returning coordinates or None at each rank."""

    if re.fullmatch(GENERATION_PATTERN, name) is None:
        return Problem("generation name is invalid", "Select a recorded generation, then retry.")

    chunks = _read_rows(
        context.host, context.production_dir, {chunk_id for pool in pools.values() for chunk_id in pool},
        label="pool chunk", statement=_CHUNKS_SQL, parse=_chunk_row,
    )
    if isinstance(chunks, Problem):
        return chunks
    manifest = _read_rows(
        context.host, context.turns_dir, {doc_id for doc_id, _, _ in chunks.values()},
        label="generation manifest", statement=_MANIFEST_SQL, parse=_manifest_row, header=bind("v_name", name),
    )
    if isinstance(manifest, Problem):
        return manifest

    resolved: dict[str, Coordinates] = {}
    for chunk_id, (doc_id, start, end) in chunks.items():
        document = manifest.get(doc_id)
        if document is not None:
            source_id, digest = document
            resolved[chunk_id] = (source_id, digest, start, end)
    return {query: tuple(resolved.get(chunk_id) for chunk_id in pool) for query, pool in pools.items()}
