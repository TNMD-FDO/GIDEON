"""Retrieve the pool's three ranked lists inside the worker over an ordered JSON wire."""

import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from types import FrameType
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

if TYPE_CHECKING:
    from gideon.host.report import Problem
    from gideon.host.sysio import Host

# E4: the release's default query-side embedding instruction, a product artifact and never a site key.
RETRIEVAL_INSTRUCTION: Final[str] = (
    "Given a legal research question, retrieve passages from court opinions, "
    "statutes, rules, and sentencing guidelines that answer it"
)
INSTRUCTED_FORM: Final[str] = "Instruct: {task}\nQuery: {query}"
# E19: the starting depth of every retrieval leg, with no score-based pre-filter.
LEG_LIMIT: Final[int] = 50
LEG_NAMES: Final = ("dense", "lexical", "controlling")
# exempt: starting bounds for fifty-three queries, corrected by measurement.
LEG_DEADLINE_SECONDS: Final[int] = 300
HOST_GRACE_SECONDS: Final[int] = 30
MARKER_PREFIX: Final[str] = "pool-leg-"
MARKER_PATTERN: Final[re.Pattern[str]] = re.compile(rf"{MARKER_PREFIX}[0-9a-f]{{12}}")
HEADER_KEYS: Final = ("generation", "served_model", "dimensions", "controlling_courts")
QUERY_KEYS: Final = ("id", "question", "scope_courts")
REPLY_KEYS: Final = ("id", *LEG_NAMES)
HIT_KEYS: Final = (
    "chunk_id", "score", "source_id", "sha256", "start", "end", "caption", "text",
)
# The evaluation set's id rule, restated for the image and held equal by test.
QUERY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"judgments-[0-9]{3,}")
# The host's generation and served-model rules, restated for the image and held equal by test.
GENERATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(caselaw)__corpus-[0-9]{4}-[0-9]{2}-[0-9]{2}__[a-z0-9]+(?:-[a-z0-9]+)*(?:__sample)?"
)
SERVED_MODEL_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]{1,64}")
# The worker's chunk ids are SHA-256 hex digests.
CHUNK_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
# The judgments file's coordinate rule, restated for the image, whose YAML-less
# Python cannot import the set's loader, and held equal by test.
SOURCE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._:/-]+")
SOURCE_ID_MAX: Final[int] = 200
SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
FAILURE_LINE: Final[re.Pattern[str]] = re.compile(
    rf"pool leg: ({QUERY_ID_PATTERN.pattern}|line [1-9][0-9]*): ([A-Za-z_][A-Za-z0-9_]*)"
)
PROBLEMS: Final[frozenset[str]] = frozenset({"pool-leg-failed"})
# The chunk id travels as _id, outside the serving copy's _source.
SERVING_FIELDS: Final = ("text", "caption", "doc_id", "char_start", "char_end")
MANIFEST_SQL: Final[str] = (
    "SELECT source_id, canonical_text_sha256, doc_id FROM generation_documents "
    "WHERE gen_id = %s AND doc_id = ANY(%s)"
)


def _object(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict) or tuple(value) != keys:
        raise ValueError("invalid wire keys")
    return value


def _query_id(value: object) -> str:
    if not isinstance(value, str) or QUERY_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid query id")
    return value


def _coordinates_valid(item: Mapping[str, object]) -> bool:
    source_id, sha256, start, end = item["source_id"], item["sha256"], item["start"], item["end"]
    return (
        isinstance(source_id, str) and 1 <= len(source_id) <= SOURCE_ID_MAX
        and SOURCE_ID_PATTERN.fullmatch(source_id) is not None
        and isinstance(sha256, str) and SHA256_PATTERN.fullmatch(sha256) is not None
        and type(start) is int and type(end) is int and 0 <= start < end
    )


def _courts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError("invalid courts")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class Hit:
    """One store hit resolved to canonical-text coordinates and its serving copy."""

    chunk_id: str
    score: float
    source_id: str
    sha256: str
    start: int
    end: int
    caption: str
    text: str

    def to_wire(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in HIT_KEYS}

    @classmethod
    def from_wire(cls, value: object) -> "Hit":
        """Refuse malformed hit fields without including any field's value."""

        item = _object(value, HIT_KEYS)
        chunk_id, score = item["chunk_id"], item["score"]
        if not isinstance(chunk_id, str) or CHUNK_ID_PATTERN.fullmatch(chunk_id) is None:
            raise ValueError("invalid chunk id")
        if type(score) not in (int, float):
            raise ValueError("invalid score")
        try:
            finite = math.isfinite(cast(float, score))
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("invalid score")
        if not _coordinates_valid(item):
            raise ValueError("invalid coordinates")
        caption, text = item["caption"], item["text"]
        if not isinstance(caption, str) or not caption.strip() or caption.splitlines() != [caption]:
            raise ValueError("invalid caption")
        start, end = cast(int, item["start"]), cast(int, item["end"])
        if not isinstance(text, str) or len(text) != end - start:
            raise ValueError("invalid text length")
        return cls(
            chunk_id, cast(float, score), cast(str, item["source_id"]),
            cast(str, item["sha256"]), start, end, caption, text,
        )


@dataclass(frozen=True, slots=True)
class LegReply:
    """A query's three ranked lists in the stores' original order."""

    query_id: str
    dense: tuple[Hit, ...]
    lexical: tuple[Hit, ...]
    controlling: tuple[Hit, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "id": self.query_id,
            **{name: [item.to_wire() for item in getattr(self, name)] for name in LEG_NAMES},
        }

    @classmethod
    def from_wire(cls, value: object) -> "LegReply":
        item = _object(value, REPLY_KEYS)
        query_id = _query_id(item["id"])
        legs: list[tuple[Hit, ...]] = []
        for name in LEG_NAMES:
            raw = item[name]
            if not isinstance(raw, list):
                raise ValueError(f"invalid {name} list")
            if len(raw) > LEG_LIMIT:
                raise ValueError(f"{name} list exceeds limit")
            hits: list[Hit] = []
            for ordinal, raw_hit in enumerate(raw, start=1):
                try:
                    hits.append(Hit.from_wire(raw_hit))
                except ValueError as error:
                    raise ValueError(f"{name} hit {ordinal}: {error}") from None
            legs.append(tuple(hits))
        return cls(query_id, *legs)


@dataclass(frozen=True, slots=True)
class LegHeader:
    """Generation and embedding facts shared by every query in a leg run."""

    generation: str
    served_model: str
    dimensions: int
    controlling_courts: tuple[str, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "served_model": self.served_model,
            "dimensions": self.dimensions,
            "controlling_courts": list(self.controlling_courts),
        }

    @classmethod
    def from_wire(cls, value: object) -> "LegHeader":
        item = _object(value, HEADER_KEYS)
        generation, served_model, dimensions = (
            item["generation"], item["served_model"], item["dimensions"],
        )
        if not isinstance(generation, str) or GENERATION_PATTERN.fullmatch(generation) is None:
            raise ValueError("invalid generation")
        if not isinstance(served_model, str) or SERVED_MODEL_PATTERN.fullmatch(served_model) is None:
            raise ValueError("invalid served model")
        if type(dimensions) is not int or dimensions <= 0:
            raise ValueError("invalid dimensions")
        return cls(generation, served_model, dimensions, _courts(item["controlling_courts"]))


@dataclass(frozen=True, slots=True)
class LegQuery:
    """One judgment question and its optional expanded court scope."""

    query_id: str
    question: str
    scope_courts: tuple[str, ...] | None

    def to_wire(self) -> dict[str, object]:
        return {
            "id": self.query_id,
            "question": self.question,
            "scope_courts": None if self.scope_courts is None else list(self.scope_courts),
        }

    @classmethod
    def from_wire(cls, value: object) -> "LegQuery":
        item = _object(value, QUERY_KEYS)
        query_id = _query_id(item["id"])
        question = item["question"]
        if not isinstance(question, str) or not question.strip():
            raise ValueError("invalid question")
        scope = item["scope_courts"]
        return cls(query_id, question, None if scope is None else _courts(scope))


def instructed(question: str) -> str:
    """Apply the embedding model's query-side form with the release's default instruction."""

    return INSTRUCTED_FORM.format(task=RETRIEVAL_INSTRUCTION, query=question)


def dense_body(
    vector: Sequence[float], courts: Iterable[str] | None, limit: int
) -> dict[str, object]:
    """Build a vector query selecting the chunk id alone from each payload."""

    body: dict[str, object] = {
        "query": list(vector), "limit": limit,
        "with_payload": ["chunk_id"], "with_vector": False,
    }
    if courts is not None:
        body["filter"] = {"must": [{"key": "court", "match": {"any": sorted(set(courts))}}]}
    return body


def lexical_body(
    question: str, courts: Iterable[str] | None, limit: int
) -> dict[str, object]:
    """Build a text match using the serving analyzer, with no collapse."""

    query: dict[str, object] = {"must": [{"match": {"text": question}}]}
    if courts is not None:
        query["filter"] = [{"terms": {"court": sorted(set(courts))}}]
    return {"query": {"bool": query}, "size": limit, "_source": list(SERVING_FIELDS)}


def hit(
    chunk_id: str, score: float, serving: Mapping[str, object], manifest_row: Mapping[str, object]
) -> Hit:
    """Resolve a serving copy through the manifest or raise a closed error."""

    if any(field not in serving for field in SERVING_FIELDS):
        raise ValueError("missing serving field")
    try:
        value = {
            "chunk_id": chunk_id, "score": score,
            "source_id": manifest_row["source_id"], "sha256": manifest_row["canonical_text_sha256"],
            "start": serving["char_start"], "end": serving["char_end"],
            "caption": serving["caption"], "text": serving["text"],
        }
    except KeyError:
        raise ValueError("missing manifest field") from None
    return Hit.from_wire(value)


def controlling_for(
    controlling: Iterable[str], scope: Iterable[str] | None
) -> tuple[str, ...]:
    """Return the controlling set under a scope; an empty result skips the leg."""

    courts = set(controlling)
    if scope is not None:
        courts.intersection_update(scope)
    return tuple(sorted(courts))


class Deadline(Exception):
    """The leg's whole-run alarm expired."""


class ManifestRowMissing(Exception):
    """An indexed document has no row in the generation's manifest."""


@dataclass(frozen=True, slots=True)
class _Clients:
    """The five requests a query makes, injectable without image dependencies."""

    embed: Callable[[str], Sequence[float]]
    vector_query: Callable[[dict[str, object]], object]
    lexical_search: Callable[[dict[str, object]], object]
    mget: Callable[[Sequence[str]], object]
    manifest_lookup: Callable[[Sequence[str]], Sequence[Mapping[str, object]]]


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("invalid store rows")
    return value


def _chunk_id(value: object) -> str:
    if not isinstance(value, str) or CHUNK_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid chunk id")
    return value


def _retrieve(header: LegHeader, query: LegQuery, clients: _Clients) -> LegReply:
    """Keep store order while joining serving copies to the generation manifest."""

    vector = clients.embed(instructed(query.question))
    dense = _rows(clients.vector_query(dense_body(vector, query.scope_courts, LEG_LIMIT)))
    controlling_courts = controlling_for(header.controlling_courts, query.scope_courts)
    controlling = (
        _rows(clients.vector_query(dense_body(vector, controlling_courts, LEG_LIMIT)))
        if controlling_courts else []
    )
    lexical = _rows(clients.lexical_search(lexical_body(query.question, query.scope_courts, LEG_LIMIT)))
    if any(len(rows) > LEG_LIMIT for rows in (dense, lexical, controlling)):
        raise ValueError("store list exceeds limit")

    def vector_id(row: Mapping[str, object]) -> str:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("missing vector payload")
        return _chunk_id(payload.get("chunk_id"))

    chunk_ids = list(dict.fromkeys(vector_id(row) for row in (*dense, *controlling)))
    copies: dict[str, dict[str, object]] = {}
    # The lexical store refuses an _mget naming no document.
    for row in _rows(clients.mget(chunk_ids)) if chunk_ids else ():
        chunk_id = _chunk_id(row.get("_id"))
        serving = row.get("_source")
        if (
            chunk_id not in chunk_ids or chunk_id in copies or row.get("found") is not True
            or "error" in row or not isinstance(serving, dict)
        ):
            raise ValueError("invalid serving copy")
        copies[chunk_id] = serving
    if set(copies) != set(chunk_ids):
        raise ValueError("missing serving copy")
    lexical_copies: list[dict[str, object]] = []
    for row in lexical:
        _chunk_id(row.get("_id"))
        serving = row.get("_source")
        if not isinstance(serving, dict):
            raise ValueError("missing lexical serving copy")
        lexical_copies.append(serving)

    def doc_id(serving: Mapping[str, object]) -> str:
        value = serving.get("doc_id")
        if not isinstance(value, str) or not value:
            raise ValueError("invalid document id")
        return value

    doc_ids = sorted({doc_id(serving) for serving in (*copies.values(), *lexical_copies)})
    manifest: dict[str, Mapping[str, object]] = {}
    for manifest_row in clients.manifest_lookup(doc_ids):
        identifier = doc_id(manifest_row)
        if identifier not in doc_ids or identifier in manifest:
            raise ValueError("invalid manifest row")
        manifest[identifier] = manifest_row
    for identifier in doc_ids:
        if identifier not in manifest:
            raise ManifestRowMissing(identifier)

    def vector_hits(rows: Sequence[Mapping[str, object]]) -> tuple[Hit, ...]:
        return tuple(
            hit(
                vector_id(row), cast(float, row["score"]), copies[vector_id(row)],
                manifest[doc_id(copies[vector_id(row)])],
            )
            for row in rows
        )

    return LegReply(
        query.query_id, vector_hits(dense),
        tuple(
            hit(_chunk_id(row["_id"]), cast(float, row["_score"]), serving, manifest[doc_id(serving)])
            for row, serving in zip(lexical, lexical_copies, strict=True)
        ),
        vector_hits(controlling),
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("invalid arguments")


def run_leg(
    host: "Host", rendered_dir: str | os.PathLike[str], header: LegHeader, queries: Sequence[LegQuery],
    *, marker: str, deadline: int,
) -> "Mapping[str, LegReply] | Problem":
    """Return replies in input order, or one Problem after a checked worker exec.

    The host's imports stay inside the function: the image imports this
    module without the host's YAML loader.
    """

    from gideon.host import stack
    from gideon.host.render.ci import EMBED_RELAY_SERVICE_NAME
    from gideon.host.render.opensearch import OPENSEARCH_SERVICE_NAME
    from gideon.host.render.qdrant import QDRANT_SERVICE_NAME
    from gideon.host.render.worker import WORKER_SERVICE_NAME
    from gideon.host.report import Problem

    fix = (
        "Check the pool leg failure line, then run "
        + "; ".join(stack.logs_fix(rendered_dir, service) for service in (
            OPENSEARCH_SERVICE_NAME, QDRANT_SERVICE_NAME, EMBED_RELAY_SERVICE_NAME,
        ))
        + ", then retry."
    )

    def failure(reason: str) -> Problem:
        return Problem(f"pool leg failed: {reason}", fix)

    def marked_pids() -> tuple[int, ...] | Problem:
        try:
            result = host.run(
                stack.compose_argv(rendered_dir, "top", WORKER_SERVICE_NAME),
                timeout=HOST_GRACE_SECONDS,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as error:
            return failure(f"leg timed out; process list could not run ({type(error).__name__})")
        if result.returncode != 0:
            return failure(f"leg timed out; process list exited {result.returncode}")
        pids: list[int] = []
        for line in result.stdout.splitlines():
            fields = line.split(maxsplit=7)
            if len(fields) != 8:
                if marker in line.split():
                    return failure("leg timed out; marked process row malformed")
                continue
            if marker not in fields[7].split():
                continue
            if not fields[1].isascii() or not fields[1].isdecimal() or int(fields[1]) <= 1:
                return failure("leg timed out; marked process pid malformed")
            pid = int(fields[1])
            if pid not in pids:
                pids.append(pid)
        return tuple(pids)

    def timed_out() -> Problem:
        pids = marked_pids()
        if isinstance(pids, Problem):
            return pids
        reason = f"leg timed out after {deadline} seconds"
        if not pids:
            return failure(reason)
        kill_failures: list[str] = []
        for pid in pids:
            try:
                killed = host.run(["kill", "-KILL", str(pid)], timeout=HOST_GRACE_SECONDS)
            except (OSError, subprocess.SubprocessError, UnicodeError) as error:
                kill_failures.append(f"kill could not run ({type(error).__name__})")
            else:
                if killed.returncode != 0:
                    kill_failures.append(f"kill exited {killed.returncode}")
        remaining = marked_pids()
        if isinstance(remaining, Problem):
            return remaining
        if kill_failures:
            reason += "; " + "; ".join(kill_failures)
        if remaining:
            reason += "; a process survives (pid " + ", ".join(map(str, remaining)) + ")"
            return Problem(
                f"pool leg failed: {reason}",
                "Run kill -9 " + " ".join(map(str, remaining)) + ". " + fix,
            )
        return failure(reason)

    if MARKER_PATTERN.fullmatch(marker) is None or type(deadline) is not int or deadline <= 0:
        return failure("invalid run marker or deadline")
    expected = tuple(query.query_id for query in queries)
    if len(set(expected)) != len(expected):
        return failure("duplicate input query id")
    try:
        request = json.dumps(header.to_wire(), ensure_ascii=False) + "\n"
        request += "".join(
            json.dumps(query.to_wire(), ensure_ascii=False) + "\n"
            for query in sorted(queries, key=lambda query: query.query_id)
        )
        result = host.run(
            stack.exec_argv(
                rendered_dir, WORKER_SERVICE_NAME, "python", "-m", "gideon.evaluation.pool_leg",
                "--run", marker, "--deadline", str(deadline),
            ),
            input=request, timeout=deadline + HOST_GRACE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return timed_out()
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        return failure(f"exec could not run ({type(error).__name__})")
    if result.returncode != 0:
        lines = result.stderr.splitlines()
        last = next((line.strip() for line in reversed(lines) if line.strip()), "")
        detail = last if FAILURE_LINE.fullmatch(last) else f"stderr {len(lines)} lines"
        return failure(f"exec exited {result.returncode}; {detail}")
    lines = result.stdout.splitlines()
    if len(lines) != len(queries):
        return failure(f"{len(lines)} reply lines for {len(queries)} queries")
    replies: dict[str, LegReply] = {}
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            return failure(f"unreadable reply line {line_number}")
        try:
            reply = LegReply.from_wire(value)
        except ValueError as error:
            return failure(f"reply line {line_number}: {error}")
        if reply.query_id not in expected:
            return failure(f"unknown reply id at line {line_number}")
        if reply.query_id in replies:
            return failure(f"repeated reply id at line {line_number}")
        replies[reply.query_id] = reply
    if set(replies) != set(expected):
        return failure("reply missing an id")
    return {query_id: replies[query_id] for query_id in expected}


def main(argv: Sequence[str] | None = None, *, clients: _Clients | None = None) -> int:
    """Read a header and queries, then emit one reply or one content-free failure."""

    location = "line 1"
    previous_handler = signal.getsignal(signal.SIGALRM)
    armed = False

    def deadline(signum: int, frame: FrameType | None) -> NoReturn:
        raise Deadline

    try:
        parser = _Parser(prog="gideon.evaluation.pool_leg")
        parser.add_argument("--run", required=True)
        parser.add_argument("--deadline", required=True, type=int)
        args = parser.parse_args(argv)
        if MARKER_PATTERN.fullmatch(args.run) is None or args.deadline <= 0:
            raise ValueError("invalid run or deadline")
        signal.signal(signal.SIGALRM, deadline)
        armed = True
        signal.alarm(args.deadline)
        header = LegHeader.from_wire(json.loads(sys.stdin.readline()))
        queries: list[LegQuery] = []
        line_number = 2
        while True:
            location = f"line {line_number}"
            line = sys.stdin.readline()
            if not line:
                break
            queries.append(LegQuery.from_wire(json.loads(line)))
            line_number += 1
        location = "line 1"
        with ExitStack() as resources:
            if clients is None:
                import httpx
                import psycopg
                from psycopg.rows import dict_row

                from gideon.worker import settings
                from gideon.worker.dense import (
                    HTTP_TIMEOUT,
                    DenseFailure,
                    HttpxEmbedder,
                )

                configuration = settings.load_settings()
                embedder = HttpxEmbedder(header.served_model, header.dimensions)
                resources.callback(embedder.close)
                lexical_client = httpx.Client(
                    base_url=configuration.opensearch_url, auth=settings.opensearch_auth(configuration),
                    timeout=HTTP_TIMEOUT, follow_redirects=False,
                )
                resources.callback(lexical_client.close)
                vector_client = httpx.Client(
                    base_url=configuration.qdrant_url, headers={"api-key": settings.qdrant_key(configuration)},
                    timeout=HTTP_TIMEOUT, follow_redirects=False,
                )
                resources.callback(vector_client.close)
                connection = psycopg.connect(
                    **cast(dict[str, Any], settings.connection_kwargs(configuration)), row_factory=dict_row,
                )
                resources.callback(connection.close)

                def embed(question: str) -> Sequence[float]:
                    reply = embedder.embed([question])
                    if len(reply.vectors) != 1:
                        raise DenseFailure("embed", error="InvalidReply")
                    return reply.vectors[0]

                def vector_query(body: dict[str, object]) -> object:
                    response = vector_client.post(f"/collections/{header.generation}/points/query", json=body)
                    response.raise_for_status()
                    value = response.json()
                    if not isinstance(value, dict) or value.get("status") != "ok":
                        raise ValueError("invalid vector reply")
                    return value["result"]["points"]

                def lexical_search(body: dict[str, object]) -> object:
                    response = lexical_client.post(f"/{header.generation}/_search", json=body)
                    response.raise_for_status()
                    value = response.json()
                    if value.get("timed_out") or value.get("_shards", {}).get("failed", 0):
                        raise ValueError("incomplete lexical reply")
                    return value["hits"]["hits"]

                def mget(ids: Sequence[str]) -> object:
                    response = lexical_client.post(
                        f"/{header.generation}/_mget", json={"ids": list(ids)},
                        params={"_source_includes": ",".join(SERVING_FIELDS)},
                    )
                    response.raise_for_status()
                    return response.json()["docs"]

                def manifest_lookup(ids: Sequence[str]) -> Sequence[Mapping[str, object]]:
                    with connection.cursor() as cursor:
                        cursor.execute(MANIFEST_SQL, (header.generation, list(ids)))
                        return cursor.fetchall()

                clients = _Clients(embed, vector_query, lexical_search, mget, manifest_lookup)
            for query in queries:
                location = query.query_id
                reply = _retrieve(header, query, clients)
                print(json.dumps(reply.to_wire(), ensure_ascii=False), flush=True)
    except Exception as error:  # noqa: BLE001 - the entry must emit no question or passage text
        print(f"pool leg: {location}: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        if armed:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
