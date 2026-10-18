"""Retrieval bodies, the ordered wire, and the pool entry over fake clients."""

import ast
import contextlib
import io
import json
import signal
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, Mock, patch

from gideon.evaluation import evalset, judgments, pool_leg
from gideon.host import stack
from gideon.host.render import worker
from gideon.host.render.ci import EMBED_RELAY_SERVICE_NAME
from gideon.host.render.opensearch import OPENSEARCH_SERVICE_NAME
from gideon.host.render.qdrant import QDRANT_SERVICE_NAME
from gideon.host.report import Problem
from gideon.host.sysio import Host

QUESTION = "Fictional research question about café jurisdiction {query}"
TEXT = "Fictitious café opinion ⚖"
CAPTION = "Fixture v. Example"
CHUNK_ID = "c" * 64
MANIFEST: dict[str, object] = {
    "source_id": "fixture:opinion/1", "canonical_text_sha256": "a" * 64,
    "doc_id": "fixture-document",
}
SERVING: dict[str, object] = {
    "text": TEXT, "caption": CAPTION, "doc_id": MANIFEST["doc_id"],
    "char_start": 7, "char_end": 7 + len(TEXT),
}


def _hit(score: float = 0.25) -> pool_leg.Hit:
    return pool_leg.hit(CHUNK_ID, score, SERVING, MANIFEST)


HEADER = pool_leg.LegHeader(
    "caselaw__corpus-2099-01-02__fixture-space__sample", "fixture-embed", 3, ("ca11", "scotus"),
)
ENTRY_ARGS = ("--run", pool_leg.MARKER_PREFIX + "a" * 12, "--deadline", "7")
CHUNK_IDS = ("b" * 64, CHUNK_ID, "d" * 64)


def _request(*queries: pool_leg.LegQuery) -> str:
    values: tuple[pool_leg.LegHeader | pool_leg.LegQuery, ...] = (HEADER, *queries)
    return "".join(json.dumps(value.to_wire()) + "\n" for value in values)


class FakeClients:
    """Five request seams holding invented store replies and a call record."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail_at: str | None = None
        self.serving = {
            chunk_id: {**SERVING, "doc_id": f"fixture-document-{index}"}
            for index, chunk_id in enumerate(CHUNK_IDS)
        }
        self.manifest = [
            {**MANIFEST, "doc_id": serving["doc_id"], "source_id": f"fixture:opinion/{index}"}
            for index, serving in enumerate(self.serving.values())
        ]
        self.dense = [
            {"id": "point-id-is-not-the-chunk-id", "score": score, "payload": {"chunk_id": chunk_id}}
            for chunk_id, score in zip(CHUNK_IDS[:2], (0.1, 0.9), strict=True)
        ]
        self.controlling = [{"score": -0.2, "payload": {"chunk_id": CHUNK_IDS[0]}}]
        self.lexical = [
            {"_id": chunk_id, "_score": score, "_source": self.serving[chunk_id]}
            for chunk_id, score in zip(reversed(CHUNK_IDS[1:]), (0.2, 0.8), strict=True)
        ]
        self.vector_replies: list[object] = [self.dense, self.controlling] * 3

    def record(self, name: str, value: object) -> None:
        self.calls.append((name, value))
        if self.fail_at == name:
            raise RuntimeError(f"{QUESTION}: {TEXT}: {CAPTION}")

    def embed(self, question: str) -> Sequence[float]:
        self.record("embed", question)
        return (0.25, -0.5, 0.75)

    def vector_query(self, body: dict[str, object]) -> object:
        self.record("vector", body)
        return self.vector_replies.pop(0)

    def lexical_search(self, body: dict[str, object]) -> object:
        self.record("lexical", body)
        return self.lexical

    def mget(self, ids: Sequence[str]) -> object:
        self.record("mget", list(ids))
        return [{"_id": key, "found": True, "_source": self.serving[key]} for key in reversed(ids)]

    def manifest_lookup(self, ids: Sequence[str]) -> Sequence[Mapping[str, object]]:
        self.record("manifest", list(ids))
        return [row for row in reversed(self.manifest) if row["doc_id"] in ids]

    def clients(self) -> pool_leg._Clients:
        return pool_leg._Clients(self.embed, self.vector_query, self.lexical_search, self.mget, self.manifest_lookup)


def _invoke(
    request: str, clients: pool_leg._Clients | None = None, args: Sequence[str] = ENTRY_ARGS,
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(request)),
        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr),
        patch.object(pool_leg.signal, "signal"), patch.object(pool_leg.signal, "alarm"),
    ):
        code = pool_leg.main(args, clients=clients)
    return code, stdout.getvalue(), stderr.getvalue()


class FakeHost:
    """The process seam returns only the queued replies, recording every call."""

    def __init__(self, *replies: subprocess.CompletedProcess[str] | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[str], str | None, float | None]] = []

    def run(
        self, argv: Sequence[str], *, input: str | None = None, timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), input, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _completed(stdout: str = "", *, stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _reply_lines(*values: object) -> str:
    return "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values)


def _top(pid: int | None = None, *, marker: str = ENTRY_ARGS[1]) -> str:
    text = (
        "fixture-worker-1\nUID PID PPID C STIME TTY TIME CMD\n"
        f"root 201 91 0 12:00 ? 00:00:00 python --run {marker}-other\n"
        f"root 202 91 0 12:00 ? 00:00:00 python --run prefix-{marker}\n"
    )
    if pid is not None:
        text += f"root {pid} 91 0 12:00 ? 00:00:00 python -m gideon.evaluation.pool_leg --run {marker} --deadline 7\n"
    return text


class QueryBodies(unittest.TestCase):
    def test_instruction_is_the_exact_query_side_form(self) -> None:
        task = (
            "Given a legal research question, retrieve passages from court opinions, "
            "statutes, rules, and sentencing guidelines that answer it"
        )
        self.assertEqual(pool_leg.RETRIEVAL_INSTRUCTION, task)
        self.assertEqual(pool_leg.INSTRUCTED_FORM, "Instruct: {task}\nQuery: {query}")
        self.assertEqual(pool_leg.instructed(QUESTION).encode(), f"Instruct: {task}\nQuery: {QUESTION}".encode())

    def test_dense_body_payload_selection_and_optional_filter(self) -> None:
        vector = (0.25, -0.5)
        expected: dict[str, object] = {
            "query": list(vector), "limit": 7,
            "with_payload": ["chunk_id"], "with_vector": False,
        }
        self.assertEqual(pool_leg.dense_body(vector, None, 7), expected)
        for courts, selected in ((["scotus", "ca11", "ca11"], ["ca11", "scotus"]), ([], [])):
            with self.subTest(courts=courts):
                self.assertEqual(pool_leg.dense_body(vector, courts, 7), {
                    **expected, "filter": {"must": [{"key": "court", "match": {"any": selected}}]},
                })

    def test_lexical_body_match_source_and_optional_terms_without_collapse(self) -> None:
        fields = ["text", "caption", "doc_id", "char_start", "char_end"]
        self.assertEqual(list(pool_leg.SERVING_FIELDS), fields)
        for courts, selected in ((None, None), (["scotus", "ca11"], ["ca11", "scotus"]), ([], [])):
            with self.subTest(courts=courts):
                query: dict[str, object] = {"must": [{"match": {"text": QUESTION}}]}
                if selected is not None:
                    query["filter"] = [{"terms": {"court": selected}}]
                self.assertEqual(pool_leg.lexical_body(QUESTION, courts, 9), {
                    "query": {"bool": query}, "size": 9, "_source": fields,
                })

    def test_controlling_intersection_and_empty_scope(self) -> None:
        controlling = ("scotus", "ca11", "alnd", "ca11")
        self.assertEqual(pool_leg.controlling_for(controlling, None), ("alnd", "ca11", "scotus"))
        self.assertEqual(pool_leg.controlling_for(controlling, ("scotus", "ca9")), ("scotus",))
        self.assertEqual(pool_leg.controlling_for(controlling, ("ca9",)), ())
        self.assertEqual(pool_leg.controlling_for(controlling, ()), ())
        self.assertEqual(pool_leg.controlling_for((), None), ())


class HitShapes(unittest.TestCase):
    def test_hit_joins_manifest_coordinates_with_unicode_serving_text(self) -> None:
        result = _hit()
        self.assertEqual(result, pool_leg.Hit(
            CHUNK_ID, 0.25, str(MANIFEST["source_id"]), str(MANIFEST["canonical_text_sha256"]),
            7, 7 + len(TEXT), CAPTION, TEXT,
        ))
        self.assertEqual(tuple(result.to_wire()), (
            "chunk_id", "score", "source_id", "sha256", "start", "end", "caption", "text",
        ))
        self.assertEqual(pool_leg.Hit.from_wire(json.loads(json.dumps(result.to_wire()))), result)

    def test_missing_serving_or_manifest_field_is_a_closed_error(self) -> None:
        for key in SERVING:
            with self.subTest(serving_key=key), self.assertRaisesRegex(ValueError, "^missing serving field$"):
                pool_leg.hit(CHUNK_ID, 0.25, {k: v for k, v in SERVING.items() if k != key}, MANIFEST)
        for key in ("source_id", "canonical_text_sha256"):
            with self.subTest(manifest_key=key), self.assertRaisesRegex(ValueError, "^missing manifest field$"):
                pool_leg.hit(CHUNK_ID, 0.25, SERVING, {k: v for k, v in MANIFEST.items() if k != key})

    def test_malformed_hit_fields_are_refused_without_values(self) -> None:
        invalid: dict[str, list[object]] = {
            "chunk_id": ["not-a-digest", "A" * 64, None],
            "score": [True, "0.25", None, float("nan"), float("inf"), -float("inf"), 10 ** 1000],
            "source_id": ["fixture invalid source", "", None],
            "sha256": ["not-a-hash", None],
            "start": [True, -1, 7.0, "7"],
            "end": [True, 7, 0, None],
            "caption": ["", " ", CAPTION + "\n", CAPTION + "\r", CAPTION + "\nsecond", None],
            "text": [TEXT + "x", TEXT[:-1], None],
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError) as caught:
                    pool_leg.Hit.from_wire({**_hit().to_wire(), key: value})
                for sentinel in (TEXT, CAPTION, "fixture invalid source", "not-a-hash"):
                    self.assertNotIn(sentinel, str(caught.exception))
        with self.assertRaisesRegex(ValueError, "invalid text length"):
            pool_leg.hit(CHUNK_ID, 0.25, {**SERVING, "char_end": 8 + len(TEXT)}, MANIFEST)


class WireShapes(unittest.TestCase):
    def setUp(self) -> None:
        self.header = pool_leg.LegHeader(
            "caselaw__corpus-2099-01-02__fixture-space__sample", "fixture-embed", 3, ("ca11", "scotus"),
        )
        self.query = pool_leg.LegQuery("judgments-001", QUESTION, None)
        self.reply = pool_leg.LegReply("judgments-001", (_hit(0.1), _hit(0.9)), (), (_hit(-0.2),))

    def test_json_round_trips_keep_key_order_and_store_order(self) -> None:
        for value, keys in (
            (self.header, ("generation", "served_model", "dimensions", "controlling_courts")),
            (self.query, ("id", "question", "scope_courts")),
            (self.reply, ("id", "dense", "lexical", "controlling")),
        ):
            with self.subTest(type=type(value)):
                wire = value.to_wire()
                self.assertEqual(tuple(wire), keys)
                self.assertEqual(type(value).from_wire(json.loads(json.dumps(wire))), value)
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, "dimensions" if isinstance(value, pool_leg.LegHeader) else "query_id", "changed")
        with self.assertRaises(FrozenInstanceError):
            _hit().score = 1.0  # type: ignore[misc]
        for scope in (None, (), ("ca11", "scotus")):
            query = pool_leg.LegQuery("judgments-001", QUESTION, scope)
            self.assertEqual(pool_leg.LegQuery.from_wire(json.loads(json.dumps(query.to_wire()))), query)

    def test_exact_ordered_keys_required_for_every_shape(self) -> None:
        for value in (self.header, self.query, self.reply, _hit()):
            wire = value.to_wire()
            for malformed in (
                None, list(wire), {**wire, "unexpected": None},
                dict(list(wire.items())[1:]), dict(reversed(list(wire.items()))),
            ):
                with self.subTest(type=type(value), malformed=malformed), self.assertRaises(ValueError):
                    type(value).from_wire(malformed)

    def test_header_refuses_malformed_fields(self) -> None:
        invalid: dict[str, list[object]] = {
            "generation": [None, "", "not-a-generation", "../fixture"],
            "served_model": [None, "", "fixture model", "x" * 65],
            "dimensions": [None, True, 0, -1, 1.5, "3"],
            "controlling_courts": [None, "scotus", [None], [""]],
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    pool_leg.LegHeader.from_wire({**self.header.to_wire(), key: value})

    def test_query_refuses_malformed_fields_without_echoing_question(self) -> None:
        invalid: dict[str, list[object]] = {
            "id": [None, "judgments-01", "segmenter-001", "judgments-001\n"],
            "question": [None, "", "  "],
            "scope_courts": ["scotus", [None], [""]],
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError) as caught:
                    pool_leg.LegQuery.from_wire({**self.query.to_wire(), key: value})
                self.assertNotIn(QUESTION, str(caught.exception))

    def test_reply_limits_lists_and_locates_bad_hit_by_ordinal(self) -> None:
        wire = self.reply.to_wire()
        for name in pool_leg.LEG_NAMES:
            maximum = {**wire, name: [_hit().to_wire()] * pool_leg.LEG_LIMIT}
            self.assertEqual(len(getattr(pool_leg.LegReply.from_wire(maximum), name)), pool_leg.LEG_LIMIT)
            bad_lists: tuple[object, ...] = (None, {}, [_hit().to_wire()] * (pool_leg.LEG_LIMIT + 1))
            for invalid in bad_lists:
                with self.subTest(name=name, invalid=type(invalid)), self.assertRaises(ValueError):
                    pool_leg.LegReply.from_wire({**wire, name: invalid})
            with self.assertRaisesRegex(ValueError, f"^{name} hit 2: invalid score$"):
                pool_leg.LegReply.from_wire({**wire, name: [_hit().to_wire(), {**_hit().to_wire(), "score": True}]})
        with self.assertRaises(ValueError):
            pool_leg.LegReply.from_wire({**wire, "id": "unknown"})
        empty = pool_leg.LegReply("judgments-001", (), (), ())
        self.assertEqual(pool_leg.LegReply.from_wire(empty.to_wire()), empty)

    def test_id_pattern_matches_evalset_and_failure_line_is_content_free(self) -> None:
        self.assertEqual(pool_leg.QUERY_ID_PATTERN.pattern, evalset.JUDGMENT_ID_PATTERN.pattern)
        self.assertEqual(pool_leg.QUERY_ID_PATTERN.flags, evalset.JUDGMENT_ID_PATTERN.flags)
        self.assertEqual(pool_leg.GENERATION_PATTERN.pattern, worker.GENERATION_PATTERN)
        self.assertEqual(pool_leg.SERVED_MODEL_PATTERN.pattern, worker.SERVED_MODEL_PATTERN)
        for location in ("judgments-001", "line 1"):
            self.assertIsNotNone(pool_leg.FAILURE_LINE.fullmatch(f"pool leg: {location}: Deadline"))
        for line in (f"pool leg: {QUESTION}: ValueError", "pool leg: line 0: ValueError", "pool leg: judgments-001: bad class"):
            self.assertIsNone(pool_leg.FAILURE_LINE.fullmatch(line))

    def test_module_level_imports_are_standard_library_only(self) -> None:
        import sys

        tree = ast.parse(Path(pool_leg.__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                self.assertIn(module.split(".")[0], sys.stdlib_module_names)

    def test_coordinate_rule_is_held_to_the_judgments_files(self) -> None:
        self.assertEqual(pool_leg.SOURCE_ID_PATTERN.pattern, judgments.SOURCE_ID_PATTERN.pattern)
        self.assertEqual(pool_leg.SHA256_PATTERN.pattern, judgments._SHA256_PATTERN.pattern)
        base = {"source_id": "fixture:opinion:1", "sha256": "a" * 64, "start": 0, "end": 1}
        variants: dict[str, list[object]] = {
            "source_id": ["fixture:opinion:1", "", "x" * 200, "x" * 201, "bad id", None, 1],
            "sha256": ["a" * 64, "A" * 64, "a" * 63, None],
            "start": [0, 5, -1, True, 1.0, None],
            "end": [1, 5, 0, True, 2.0, None],
        }
        for key, values in variants.items():
            for value in values:
                item = {**base, key: value}
                with self.subTest(key=key, value=value):
                    self.assertEqual(pool_leg._coordinates_valid(item), not judgments.coordinate_findings(item))

    def test_a_hit_resolves_where_yaml_cannot_be_imported(self) -> None:
        script = (
            "import sys\n"
            "sys.modules['yaml'] = None\n"
            "from gideon.evaluation import pool_leg\n"
            f"serving = {SERVING!r}\n"
            f"manifest = {MANIFEST!r}\n"
            f"hit = pool_leg.hit({CHUNK_ID!r}, 0.5, serving, manifest)\n"
            "assert pool_leg.Hit.from_wire(hit.to_wire()) == hit\n"
            "assert 'gideon.evaluation.evalset' not in sys.modules\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=Path(pool_leg.__file__).parents[2],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


class Entry(unittest.TestCase):
    def test_query_sequence_filters_and_store_order(self) -> None:
        fake = FakeClients()
        queries = (
            pool_leg.LegQuery("judgments-001", QUESTION, None),
            pool_leg.LegQuery("judgments-002", QUESTION + " second", ("ca11", "scotus")),
        )
        code, stdout, stderr = _invoke(_request(*queries), fake.clients())
        self.assertEqual((code, stderr), (0, ""))
        replies = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [query.query_id for query in queries])
        sequence = ["embed", "vector", "vector", "lexical", "mget", "manifest"]
        self.assertEqual([name for name, _ in fake.calls], sequence * len(queries))
        for index, query in enumerate(queries):
            calls = fake.calls[index * len(sequence):(index + 1) * len(sequence)]
            self.assertEqual(calls[0][1], pool_leg.instructed(query.question))
            self.assertEqual(calls[1][1], pool_leg.dense_body((0.25, -0.5, 0.75), query.scope_courts, pool_leg.LEG_LIMIT))
            self.assertEqual(calls[2][1], pool_leg.dense_body((0.25, -0.5, 0.75), HEADER.controlling_courts, pool_leg.LEG_LIMIT))
            self.assertEqual(calls[3][1], pool_leg.lexical_body(query.question, query.scope_courts, pool_leg.LEG_LIMIT))
            self.assertEqual(calls[4][1], list(CHUNK_IDS[:2]))
            self.assertEqual(calls[5][1], sorted(str(row["doc_id"]) for row in fake.manifest))
            reply = replies[index]
            self.assertEqual(tuple(reply), pool_leg.REPLY_KEYS)
            self.assertEqual([row["chunk_id"] for row in reply["dense"]], list(CHUNK_IDS[:2]))
            self.assertEqual([row["score"] for row in reply["dense"]], [0.1, 0.9])
            self.assertEqual([row["chunk_id"] for row in reply["lexical"]], list(reversed(CHUNK_IDS[1:])))
            self.assertEqual([row["score"] for row in reply["lexical"]], [0.2, 0.8])
            self.assertEqual([row["score"] for row in reply["controlling"]], [-0.2])
            for name in pool_leg.LEG_NAMES:
                for row in reply[name]:
                    self.assertEqual(tuple(row), pool_leg.HIT_KEYS)
                    serving = fake.serving[row["chunk_id"]]
                    manifest = next(item for item in fake.manifest if item["doc_id"] == serving["doc_id"])
                    self.assertEqual(row["source_id"], manifest["source_id"])
                    self.assertEqual(row["sha256"], manifest["canonical_text_sha256"])
                    self.assertEqual(row["text"], TEXT)
            pool_leg.LegReply.from_wire(reply)

    def test_empty_controlling_intersection_skips_only_that_query(self) -> None:
        fake = FakeClients()
        query = pool_leg.LegQuery("judgments-001", QUESTION, ("ca9",))
        code, stdout, stderr = _invoke(_request(query), fake.clients())
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual([name for name, _ in fake.calls], ["embed", "vector", "lexical", "mget", "manifest"])
        self.assertEqual(json.loads(stdout)["controlling"], [])

    def test_empty_lists_still_reply_and_send_no_empty_mget(self) -> None:
        fake = FakeClients()
        fake.vector_replies = [[], []]
        fake.lexical = []
        query = pool_leg.LegQuery("judgments-001", QUESTION, None)
        code, stdout, stderr = _invoke(_request(query), fake.clients())
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout), {"id": query.query_id, "dense": [], "lexical": [], "controlling": []})
        self.assertEqual([name for name, _ in fake.calls], ["embed", "vector", "vector", "lexical", "manifest"])

    def test_missing_manifest_row_fails_by_query_id_with_its_own_class(self) -> None:
        fake = FakeClients()
        missing = fake.manifest.pop()
        query = pool_leg.LegQuery("judgments-001", QUESTION, None)
        code, stdout, stderr = _invoke(_request(query), fake.clients())
        self.assertEqual((code, stdout, stderr), (1, "", "pool leg: judgments-001: ManifestRowMissing\n"))
        fake = FakeClients()
        fake.manifest = [row for row in fake.manifest if row["doc_id"] != missing["doc_id"]]
        with self.assertRaises(pool_leg.ManifestRowMissing) as caught:
            pool_leg._retrieve(HEADER, query, fake.clients())
        self.assertEqual(caught.exception.args, (missing["doc_id"],))

    def test_each_request_failure_is_one_line_without_content(self) -> None:
        for stage in ("embed", "vector", "lexical", "mget", "manifest"):
            fake = FakeClients()
            fake.fail_at = stage
            with self.subTest(stage=stage):
                code, stdout, stderr = _invoke(_request(pool_leg.LegQuery("judgments-001", QUESTION, None)), fake.clients())
                self.assertEqual((code, stdout, stderr), (1, "", "pool leg: judgments-001: RuntimeError\n"))
                self.assertEqual(fake.calls[-1][0], stage)

    def test_bad_header_or_any_bad_query_refuses_before_any_request(self) -> None:
        valid = pool_leg.LegQuery("judgments-001", QUESTION, None)
        header_line = json.dumps(HEADER.to_wire()) + "\n"
        cases = (
            ("", "line 1", "JSONDecodeError"),
            ("not json\n", "line 1", "JSONDecodeError"),
            (json.dumps({**HEADER.to_wire(), "dimensions": True}) + "\n", "line 1", "ValueError"),
            (header_line + json.dumps({**valid.to_wire(), "scope_courts": [None]}) + "\n", "line 2", "ValueError"),
            (_request(valid) + "not json\n", "line 3", "JSONDecodeError"),
            (header_line + json.dumps(dict(reversed(list(valid.to_wire().items())))) + "\n", "line 2", "ValueError"),
        )
        for request, location, exception in cases:
            fake = FakeClients()
            with self.subTest(location=location, exception=exception):
                code, stdout, stderr = _invoke(request, fake.clients())
                self.assertEqual((code, stdout, stderr), (1, "", f"pool leg: {location}: {exception}\n"))
                self.assertEqual(fake.calls, [])

    def test_arguments_are_validated_without_echoing_values_or_reading_stdin(self) -> None:
        marker = ENTRY_ARGS[1]
        invalid = (
            (), ("--deadline", "7"), ("--run", marker),
            ("--run", marker, "--deadline", "0"), ("--run", marker, "--deadline", "-1"),
            ("--run", marker, "--deadline", "1.5"), ("--run", marker, "--deadline", QUESTION),
            ("--run", QUESTION, "--deadline", "7"),
            ("--run", pool_leg.MARKER_PREFIX + "a" * 11, "--deadline", "7"),
            ("--run", pool_leg.MARKER_PREFIX + "A" * 12, "--deadline", "7"),
            (*ENTRY_ARGS, "--unknown", QUESTION),
        )
        for args in invalid:
            fake = FakeClients()
            with self.subTest(args=args):
                code, stdout, stderr = _invoke("", fake.clients(), args)
                self.assertEqual((code, stdout, stderr), (1, "", "pool leg: line 1: ValueError\n"))
                self.assertEqual(fake.calls, [])

    def test_alarm_precedes_first_read_and_handler_raises_deadline(self) -> None:
        events: list[tuple[str, object]] = []
        handlers: list[object] = []

        def install(signum: int, handler: object) -> None:
            self.assertEqual(signum, signal.SIGALRM)
            handlers.append(handler)
            events.append(("handler", handler))

        class StalledInput:
            def readline(self, size: int = -1) -> str:
                events.append(("read", None))
                handler = handlers[0]
                assert callable(handler)
                handler(signal.SIGALRM, None)
                raise AssertionError("deadline handler returned")

        stdout, stderr = io.StringIO(), io.StringIO()
        previous = signal.getsignal(signal.SIGALRM)
        with (
            patch("sys.stdin", StalledInput()), contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr), patch.object(pool_leg.signal, "signal", side_effect=install),
            patch.object(pool_leg.signal, "alarm", side_effect=lambda seconds: events.append(("alarm", seconds))),
        ):
            code = pool_leg.main(ENTRY_ARGS, clients=FakeClients().clients())
        self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (1, "", "pool leg: line 1: Deadline\n"))
        self.assertEqual([name for name, _ in events], ["handler", "alarm", "read", "alarm", "handler"])
        self.assertEqual(events[1], ("alarm", int(ENTRY_ARGS[3])))
        self.assertEqual(events[3], ("alarm", 0))
        self.assertEqual(handlers[-1], previous)


class ImageClients(unittest.TestCase):
    """Exercise the real adapters in main with HTTP and database construction faked."""

    def setUp(self) -> None:
        import httpx

        from gideon.worker import dense

        self.fake = FakeClients()
        self.configuration = SimpleNamespace(opensearch_url="http://lexical.example:9200", qdrant_url="http://vector.example:6333")
        self.embedder = Mock()
        self.embedder.embed.return_value = dense.EmbedReply(((0.25, -0.5, 0.75),), 3)
        self.lexical = Mock()
        self.vector = Mock()
        self.vector.post.side_effect = [
            httpx.Response(200, json={"status": "ok", "result": {"points": rows}}, request=httpx.Request("POST", "http://vector.example"))
            for rows in (self.fake.dense, self.fake.controlling)
        ]
        self.lexical.post.side_effect = [
            httpx.Response(200, json={"hits": {"hits": self.fake.lexical}}, request=httpx.Request("POST", "http://lexical.example")),
            httpx.Response(200, json={"docs": self.fake.mget(CHUNK_IDS[:2])}, request=httpx.Request("POST", "http://lexical.example")),
        ]
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchall.return_value = self.fake.manifest
        self.settings_loader = self.enterContext(patch("gideon.worker.settings.load_settings", return_value=self.configuration))
        self.auth = ("fixture-user", "fixture-password")
        self.enterContext(patch("gideon.worker.settings.opensearch_auth", return_value=self.auth))
        self.enterContext(patch("gideon.worker.settings.qdrant_key", return_value="fixture-key"))
        self.db_kwargs = {"dbname": "fixture-record", "user": "fixture-worker"}
        self.enterContext(patch("gideon.worker.settings.connection_kwargs", return_value=self.db_kwargs))
        self.embed_factory = self.enterContext(patch("gideon.worker.dense.HttpxEmbedder", return_value=self.embedder))
        self.http_factory = self.enterContext(patch("httpx.Client", side_effect=[self.lexical, self.vector]))
        self.connect = self.enterContext(patch("psycopg.connect", return_value=self.connection))

    def assert_closed(self) -> None:
        for resource in (self.embedder, self.lexical, self.vector, self.connection):
            resource.close.assert_called_once_with()

    def test_real_adapters_use_worker_settings_and_one_request_per_store_operation(self) -> None:
        from psycopg.rows import dict_row

        from gideon.worker.dense import HTTP_TIMEOUT

        code, stdout, stderr = _invoke(_request(pool_leg.LegQuery("judgments-001", QUESTION, None)))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["id"], "judgments-001")
        self.settings_loader.assert_called_once_with()
        self.embed_factory.assert_called_once_with(HEADER.served_model, HEADER.dimensions)
        self.embedder.embed.assert_called_once_with([pool_leg.instructed(QUESTION)])
        self.assertEqual(self.http_factory.call_count, 2)
        self.assertEqual(self.http_factory.call_args_list[0].kwargs, {
            "base_url": self.configuration.opensearch_url, "auth": self.auth,
            "timeout": HTTP_TIMEOUT, "follow_redirects": False,
        })
        self.assertEqual(self.http_factory.call_args_list[1].kwargs, {
            "base_url": self.configuration.qdrant_url, "headers": {"api-key": "fixture-key"},
            "timeout": HTTP_TIMEOUT, "follow_redirects": False,
        })
        self.connect.assert_called_once_with(**self.db_kwargs, row_factory=dict_row)
        self.assertEqual([call.args[0] for call in self.vector.post.call_args_list], [f"/collections/{HEADER.generation}/points/query"] * 2)
        self.assertEqual([call.args[0] for call in self.lexical.post.call_args_list], [f"/{HEADER.generation}/_search", f"/{HEADER.generation}/_mget"])
        self.assertEqual(self.lexical.post.call_args_list[1].kwargs, {
            "json": {"ids": list(CHUNK_IDS[:2])}, "params": {"_source_includes": ",".join(pool_leg.SERVING_FIELDS)},
        })
        self.cursor.execute.assert_called_once_with(
            pool_leg.MANIFEST_SQL, (HEADER.generation, sorted(str(row["doc_id"]) for row in self.fake.manifest)),
        )
        self.assertIn("FROM generation_documents WHERE gen_id = %s AND doc_id = ANY(%s)", pool_leg.MANIFEST_SQL)
        self.assert_closed()

    def test_store_failures_do_not_retry_and_every_resource_closes(self) -> None:
        import httpx

        for status in (429, 503):
            with self.subTest(status=status):
                for resource in (self.embedder, self.lexical, self.vector, self.connection):
                    resource.close.reset_mock()
                self.http_factory.side_effect = [self.lexical, self.vector]
                self.vector.post.reset_mock()
                self.vector.post.side_effect = [
                    httpx.Response(status, text=TEXT, request=httpx.Request("POST", "http://vector.example")),
                ]
                code, stdout, stderr = _invoke(_request(pool_leg.LegQuery("judgments-001", QUESTION, None)))
                self.assertEqual((code, stdout, stderr), (1, "", "pool leg: judgments-001: HTTPStatusError\n"))
                self.assertEqual(self.vector.post.call_count, 1)
                self.assert_closed()

    def test_deadline_during_request_closes_resources_and_names_query(self) -> None:
        self.embedder.embed.side_effect = pool_leg.Deadline
        code, stdout, stderr = _invoke(_request(pool_leg.LegQuery("judgments-001", QUESTION, None)))
        self.assertEqual((code, stdout, stderr), (1, "", "pool leg: judgments-001: Deadline\n"))
        self.assert_closed()

    def test_partial_client_construction_failure_closes_created_clients(self) -> None:
        self.http_factory.side_effect = [self.lexical, RuntimeError(TEXT)]
        code, stdout, stderr = _invoke(_request(pool_leg.LegQuery("judgments-001", QUESTION, None)))
        self.assertEqual((code, stdout, stderr), (1, "", "pool leg: line 1: RuntimeError\n"))
        self.embedder.close.assert_called_once_with()
        self.lexical.close.assert_called_once_with()
        self.connect.assert_not_called()

    def test_malformed_input_does_not_construct_clients(self) -> None:
        code, stdout, stderr = _invoke("{}\n")
        self.assertEqual((code, stdout, stderr), (1, "", "pool leg: line 1: ValueError\n"))
        self.settings_loader.assert_not_called()
        self.http_factory.assert_not_called()
        self.embed_factory.assert_not_called()
        self.connect.assert_not_called()


class HostLeg(unittest.TestCase):
    def setUp(self) -> None:
        self.rendered_dir = Path("/tmp/fixture-pool-rendered")
        self.marker = ENTRY_ARGS[1]
        self.deadline = int(ENTRY_ARGS[3])
        self.query = pool_leg.LegQuery("judgments-001", QUESTION, None)
        self.reply = pool_leg.LegReply(self.query.query_id, (_hit(0.1), _hit(0.9)), (), (_hit(-0.2),))

    def run_host(
        self, host: FakeHost, queries: Sequence[pool_leg.LegQuery] | None = None,
    ) -> Mapping[str, pool_leg.LegReply] | Problem:
        return pool_leg.run_leg(
            cast(Host, host), self.rendered_dir, HEADER, (self.query,) if queries is None else queries,
            marker=self.marker, deadline=self.deadline,
        )

    def assert_problem(self, value: object, *details: str) -> Problem:
        self.assertIsInstance(value, Problem)
        assert isinstance(value, Problem)
        for detail in details:
            self.assertIn(detail, value.problem)
        for service in (OPENSEARCH_SERVICE_NAME, QDRANT_SERVICE_NAME, EMBED_RELAY_SERVICE_NAME):
            self.assertIn(stack.logs_fix(self.rendered_dir, service), value.fix)
        self.assertIn("failure line", value.fix)
        self.assertIn("retry", value.fix)
        for sentinel in (QUESTION, TEXT, CAPTION, "unsafe-id", "private diagnostic"):
            self.assertNotIn(sentinel, value.problem)
            self.assertNotIn(sentinel, value.fix)
        return value

    def test_exec_argv_request_deadline_and_return_order(self) -> None:
        queries = (pool_leg.LegQuery("judgments-002", QUESTION + " second", ("ca11",)), self.query)
        second = pool_leg.LegReply("judgments-002", (), (_hit(0.3),), ())
        host = FakeHost(_completed(_reply_lines(self.reply.to_wire(), second.to_wire())))
        result = self.run_host(host, queries)
        assert not isinstance(result, Problem)
        self.assertEqual(tuple(result), tuple(query.query_id for query in queries))
        self.assertEqual(result, {second.query_id: second, self.reply.query_id: self.reply})
        self.assertEqual(host.calls, [(
            stack.exec_argv(
                self.rendered_dir, worker.WORKER_SERVICE_NAME, "python", "-m", "gideon.evaluation.pool_leg",
                "--run", self.marker, "--deadline", str(self.deadline),
            ),
            _reply_lines(HEADER.to_wire(), self.query.to_wire(), queries[0].to_wire()),
            self.deadline + pool_leg.HOST_GRACE_SECONDS,
        )])
        request = host.calls[0][1]
        assert request is not None
        self.assertEqual([tuple(json.loads(line)) for line in request.splitlines()], [pool_leg.HEADER_KEYS, pool_leg.QUERY_KEYS, pool_leg.QUERY_KEYS])

    def test_exec_failure_reads_only_a_matching_last_nonempty_stderr_line(self) -> None:
        for location in (self.query.query_id, "line 2"):
            safe = f"pool leg: {location}: ManifestRowMissing"
            host = FakeHost(_completed(TEXT, stderr=f"private diagnostic\n{QUESTION}\n{safe}\n\n  \n", code=1))
            with self.subTest(location=location):
                self.assert_problem(self.run_host(host), "exec exited 1", safe)
                self.assertEqual(len(host.calls), 1)

    def test_nonmatching_stderr_is_counted_without_echoing_even_an_earlier_match(self) -> None:
        safe = f"pool leg: {self.query.query_id}: ValueError"
        for stderr, count in (("", 0), (f"{safe}\nprivate diagnostic\n\n", 3), (QUESTION + "\n", 1)):
            with self.subTest(count=count):
                result = self.assert_problem(self.run_host(FakeHost(_completed(TEXT, stderr=stderr, code=17))), "exec exited 17", f"stderr {count} lines")
                self.assertNotIn(safe, result.problem)

    def test_exec_cannot_run_is_one_content_free_refusal(self) -> None:
        for error in (OSError(QUESTION), subprocess.SubprocessError(TEXT), UnicodeError(CAPTION)):
            with self.subTest(error=type(error).__name__):
                self.assert_problem(self.run_host(FakeHost(error)), "exec could not run", type(error).__name__)

    def test_reply_counts_ids_and_json_fail_closed(self) -> None:
        wire = self.reply.to_wire()
        cases = (
            ("", "0 reply lines"),
            (_reply_lines(wire, wire), "2 reply lines"),
            (QUESTION + "\n", "unreadable reply line 1"),
            (_reply_lines({**wire, "id": "judgments-999"}), "unknown reply id at line 1"),
            (_reply_lines({**wire, "id": "unsafe-id"}), "reply line 1: invalid query id"),
            (_reply_lines({**wire, "id": None}), "reply line 1: invalid query id"),
        )
        for stdout, reason in cases:
            with self.subTest(reason=reason):
                self.assert_problem(self.run_host(FakeHost(_completed(stdout))), reason)
        queries = (self.query, pool_leg.LegQuery("judgments-002", QUESTION, None))
        self.assert_problem(self.run_host(FakeHost(_completed(_reply_lines(wire, wire))), queries), "repeated reply id at line 2")
        self.assert_problem(self.run_host(FakeHost(_completed(_reply_lines(wire))), queries), "1 reply lines for 2 queries")

    def test_reply_and_hit_ordered_keys_are_closed(self) -> None:
        wire = self.reply.to_wire()
        invalid_replies: tuple[object, ...] = (None, [], {**wire, "unexpected": TEXT}, dict(reversed(list(wire.items()))), {key: value for key, value in wire.items() if key != "dense"})
        for invalid in invalid_replies:
            with self.subTest(reply=invalid):
                self.assert_problem(self.run_host(FakeHost(_completed(_reply_lines(invalid)))), "reply line 1", "invalid wire keys")
        raw_hit = _hit().to_wire()
        invalid_hits: tuple[object, ...] = (None, [], {**raw_hit, "unexpected": TEXT}, dict(reversed(list(raw_hit.items()))), {key: value for key, value in raw_hit.items() if key != "text"})
        for invalid in invalid_hits:
            value = {**wire, "dense": [raw_hit, invalid]}
            with self.subTest(hit=invalid):
                self.assert_problem(self.run_host(FakeHost(_completed(_reply_lines(value)))), "reply line 1: dense hit 2", "invalid wire keys")

    def test_each_leg_limit_is_checked_and_empty_legs_are_valid(self) -> None:
        wire = self.reply.to_wire()
        for name in pool_leg.LEG_NAMES:
            invalid_legs: tuple[object, ...] = (None, {}, [_hit().to_wire()] * (pool_leg.LEG_LIMIT + 1))
            for raw in invalid_legs:
                with self.subTest(leg=name, raw=type(raw).__name__):
                    reason = "exceeds limit" if isinstance(raw, list) else f"invalid {name} list"
                    self.assert_problem(self.run_host(FakeHost(_completed(_reply_lines({**wire, name: raw})))), "reply line 1", name, reason)
        empty = pool_leg.LegReply(self.query.query_id, (), (), ())
        self.assertEqual(self.run_host(FakeHost(_completed(_reply_lines(empty.to_wire())))), {empty.query_id: empty})

    def test_coordinates_chunk_id_caption_score_and_text_fail_by_line_leg_and_ordinal(self) -> None:
        invalid: dict[str, tuple[list[object], str]] = {
            "chunk_id": (["unsafe-id", "A" * 64, None], "invalid chunk id"),
            "source_id": (["unsafe-id with spaces", "", None], "invalid coordinates"),
            "sha256": (["unsafe-id", "A" * 64, None], "invalid coordinates"),
            "start": ([-1, True, 7.0, "7"], "invalid coordinates"),
            "end": ([7, True, 0, None], "invalid coordinates"),
            "caption": (["", " ", CAPTION + "\n", CAPTION + "\nsecond", None], "invalid caption"),
            "score": ([True, "0.3", None, float("nan"), float("inf"), -float("inf")], "invalid score"),
            "text": ([TEXT + "x", TEXT[:-1], None], "invalid text length"),
        }
        for key, (values, reason) in invalid.items():
            for value in values:
                for name in pool_leg.LEG_NAMES:
                    wire = {**self.reply.to_wire(), name: [_hit().to_wire(), {**_hit().to_wire(), key: value}]}
                    with self.subTest(key=key, value=value, leg=name):
                        self.assert_problem(self.run_host(FakeHost(_completed(_reply_lines(wire)))), f"reply line 1: {name} hit 2", reason)

    def test_timeout_without_a_marked_row_confirms_ended_without_killing_lookalikes(self) -> None:
        host = FakeHost(subprocess.TimeoutExpired([], self.deadline), _completed(_top()))
        self.assert_problem(self.run_host(host), "leg timed out", f"{self.deadline} seconds")
        self.assertEqual(len(host.calls), 2)
        self.assertEqual(host.calls[1], (stack.compose_argv(self.rendered_dir, "top", worker.WORKER_SERVICE_NAME), None, pool_leg.HOST_GRACE_SECONDS))

    def test_timeout_kills_the_host_pid_then_rechecks_the_marker(self) -> None:
        pid = 32123
        host = FakeHost(subprocess.TimeoutExpired([], self.deadline), _completed(_top(pid)), _completed(), _completed(_top()))
        result = self.assert_problem(self.run_host(host), "leg timed out", f"{self.deadline} seconds")
        self.assertNotIn("survives", result.problem)
        self.assertEqual(host.calls[2], (["kill", "-KILL", str(pid)], None, pool_leg.HOST_GRACE_SECONDS))
        self.assertEqual(host.calls[1], host.calls[3])
        self.assertEqual(len(host.calls), 4)

    def test_timeout_survivor_has_a_pid_and_kill_fix(self) -> None:
        pid = 32123
        host = FakeHost(subprocess.TimeoutExpired([], self.deadline), _completed(_top(pid)), _completed(), _completed(_top(pid)))
        result = self.assert_problem(self.run_host(host), "leg timed out", f"a process survives (pid {pid})")
        self.assertIn(f"kill -9 {pid}", result.fix)
        self.assertEqual(len(host.calls), 4)

    def test_failed_top_is_refused_even_when_its_stdout_has_a_marked_process(self) -> None:
        for response in (_completed(_top(32123), stderr=QUESTION, code=19), OSError(TEXT), subprocess.TimeoutExpired([], 1)):
            host = FakeHost(subprocess.TimeoutExpired([], self.deadline), response)
            with self.subTest(response=type(response).__name__):
                self.assert_problem(self.run_host(host), "leg timed out", "process list")
                self.assertEqual(len(host.calls), 2)

    def test_kill_errors_are_judged_and_a_recheck_still_confirms_the_outcome(self) -> None:
        for response, reason in ((_completed(stderr=QUESTION, code=13), "kill exited 13"), (OSError(TEXT), "kill could not run (OSError)")):
            for survives in (False, True):
                host = FakeHost(subprocess.TimeoutExpired([], self.deadline), _completed(_top(32123)), response, _completed(_top(32123 if survives else None)))
                with self.subTest(reason=reason, survives=survives):
                    result = self.assert_problem(self.run_host(host), reason)
                    self.assertEqual("survives" in result.problem, survives)
                    self.assertEqual(len(host.calls), 4)

    def test_failed_recheck_and_malformed_marked_pids_refuse(self) -> None:
        host = FakeHost(subprocess.TimeoutExpired([], self.deadline), _completed(_top(32123)), _completed(), _completed(code=23))
        self.assert_problem(self.run_host(host), "process list exited 23")
        for pid in ("unsafe-id", "-1", "0", "1"):
            host = FakeHost(subprocess.TimeoutExpired([], self.deadline), _completed(_top(32123).replace("32123", pid)))
            with self.subTest(pid=pid):
                self.assert_problem(self.run_host(host), "pid malformed")
                self.assertEqual(len(host.calls), 2)

    def test_host_worker_and_client_imports_live_inside_functions(self) -> None:
        tree = ast.parse(Path(pool_leg.__file__).read_text(encoding="utf-8"))
        found: set[str] = set()

        def walk(node: ast.AST, in_function: bool = False) -> None:
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [(node.module or "") + "." + alias.name for alias in node.names]
            for module in modules:
                for prefix in ("gideon.host", "gideon.worker", "httpx", "psycopg"):
                    if module == prefix or module.startswith(prefix + "."):
                        self.assertTrue(in_function, f"{module} imported outside a function")
                        found.add(prefix)
            # A TYPE_CHECKING block never runs, so the image never meets its imports.
            type_only = (
                isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
            )
            for child in ast.iter_child_nodes(node):
                walk(child, in_function or type_only or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))

        walk(tree)
        self.assertEqual(found, {"gideon.host", "gideon.worker", "httpx", "psycopg"})
