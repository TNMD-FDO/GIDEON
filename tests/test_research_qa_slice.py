"""Research question scoring over encoded turns and metrics-role record reads."""

import ast
import json
import subprocess
import sys
import unittest
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

from test_evaluation_record import read_psql_set
from test_host_import_boundary import absolute_targets, iter_imports
from test_judge import PROMPT, engine_output, held_judge_retry, valid_content
from test_research_door import stream_envelope, turn_pool_fixture
from test_turns_door import FakeHost

from gideon.evaluation import baselines, record, research_qa_slice
from gideon.evaluation.evalset import SET_ROOT, Case, LoadedSet, load_set, select_cases
from gideon.evaluation.generation import GenerationAccess
from gideon.evaluation.rankmetrics import Coordinates
from gideon.evaluation.research_door import TURN_PATH
from gideon.evaluation.results import JSONValue, RunContext, SliceResult
from gideon.evaluation.signoffs import SignOff
from gideon.evaluation.slices import SLICE_RUNNERS
from gideon.evaluation.turns import doorclient
from gideon.guardrail.writer import bounded_id
from gideon.host import stack
from gideon.host.render.engine import GENERATOR
from gideon.host.sysio import Command, PathLike
from gideon.turn import events
from gideon.turn.states import State, TurnFailure

RETRIEVAL = "research-qa-retrieval"
SYNTHESIS = "research-qa-synthesis"
QUESTION = "Fictitious confidential research question"
PASSAGE = "Fictitious confidential quoted passage"
ANSWER = "Fictitious confidential terminal answer"
REFERENCE = "Fictitious confidential reference answer"
SIGNED_ANSWER = "Fictitious confidential signed answer"
REASON = "Fictitious confidential grading reason"
DIAGNOSTIC = "Fictitious confidential record diagnostic"
TOTAL_MS = 1234.5
ELAPSED = 2.5


class ResearchQAHost(FakeHost):
    """Queue Research streams and serve their pool and gold rows from dicts."""

    def __init__(self, overrides: Mapping[str, JSONValue] | None = None) -> None:
        super().__init__()
        generation = turn_pool_fixture("reply.json")["generation"]
        self.generation = GenerationAccess(generation["gen_id"], generation["lockfiles"][0], "live")
        config_hash = baselines.expected_config_hash(overrides, reranker=True)
        assert isinstance(config_hash, str)
        self.config_hash = config_hash
        self.overrides = overrides
        self.turns_dir = Path("/fictitious/research-stack")
        self.production_dir = Path("/fictitious/record-stack")
        self.responses: deque[subprocess.CompletedProcess[str]] = deque()
        self.judge_responses: deque[subprocess.CompletedProcess[str]] = deque()
        self.judge_requests: list[dict[str, object]] = []
        self.documents: dict[str, tuple[str, str]] = {}
        self.chunks: dict[str, tuple[str, int, int]] = {}
        self.refused_read: str | None = None
        self.reads: list[tuple[str, tuple[str, ...]]] = []
        self.activity: list[str] = []
        self.message_ids: list[str] = []
        self.progress: list[str] = []

    def context(self, *, judge_prompt_id: str | None = None) -> RunContext:
        context = RunContext(
            self, self.turns_dir, self.production_dir,
            "fictitious-synthesis-generator" if judge_prompt_id is not None else None,
            judge_prompt_id, 1, self.progress.append,
            generation=self.generation,
            turn_overrides=self.overrides,
            expected_config_hash=self.config_hash,
            judge_retry=None if judge_prompt_id is None else held_judge_retry(),
            checkpoint=lambda: self.activity.append("checkpoint"),
        )

        def mint() -> str:
            self.activity.append("mint")
            message_id = context.message_id_factory()
            self.message_ids.append(message_id)
            return message_id

        return replace(context, message_id_factory=mint)

    def turn_questions(self) -> list[str]:
        return [cast(Mapping[str, str], request["body"])["message"] for request in self.requests]

    def add_document(self, source_id: str, digest: str) -> str:
        doc_id = sha256(source_id.encode()).hexdigest()
        self.documents[doc_id] = source_id, digest
        return doc_id

    def add_chunk(self, coordinate: Coordinates) -> str:
        source_id, digest, start, end = coordinate
        chunk_id = sha256(f"fictitious chunk {len(self.chunks)}".encode()).hexdigest()
        self.chunks[chunk_id] = self.add_document(source_id, digest), start, end
        return chunk_id

    def queue_turn(
        self, pool: Sequence[str], evidence: Sequence[str] = (), *,
        end: events.EndWord = "complete", generation: GenerationAccess | None = None,
        config_hash: str | None = None,
    ) -> None:
        """Encode a turn with fused ranks reversed relative to its ranked pool."""

        generation = self.generation if generation is None else generation
        fused = {chunk_id: rank for rank, chunk_id in enumerate(reversed(pool), start=1)}
        retrieval = {
            "stage": "retrieve",
            "generation": {"gen_id": generation.name, "lockfiles": [generation.corpus_lockfile]},
            "retrieval_config_hash": self.config_hash if config_hash is None else config_hash,
            "pool": [
                {"chunk_id": chunk_id, "rank": rank, "pool_rank": fused[chunk_id]}
                for rank, chunk_id in enumerate(pool, start=1)
            ],
            "timings_ms": {"total": 18.0},
            "support_level": "ok",
        }
        frames = (
            *(events.passage(number, chunk_id, fused[chunk_id])
              for number, chunk_id in enumerate(evidence, start=1)),
            events.phase("retrieve", retrieval),
            events.phase("generate", 1),
            events.text(PASSAGE),
            events.answer(
                ANSWER, end, {"total": TOTAL_MS},
                family="fictitious-family" if end == "tripped" else None,
            ),
        )
        self.responses.append(stream_envelope(frames, ELAPSED))

    def queue_gold_turn(self, case: Case, *, end: events.EndWord = "complete") -> None:
        pool = tuple(
            self.add_chunk((
                cast(str, gold["source_id"]), cast(str, gold["sha256"]),
                cast(int, gold["start"]), cast(int, gold["end"]),
            ))
            for gold in cast(list[dict[str, object]], case["gold"])
        )
        self.queue_turn(pool, pool, end=end)

    def queue_failure(self, state: State) -> None:
        failure = TurnFailure(state, "generate").to_body()
        failure["error"]["message"] = ANSWER
        self.responses.append(stream_envelope((events.encode("failure", failure),), ELAPSED))

    def queue_grade(self, stdout: str, *, returncode: int = 0) -> None:
        self.judge_responses.append(subprocess.CompletedProcess(("fictitious",), returncode, stdout, DIAGNOSTIC))

    def run(
        self, argv: Command, *, check: bool = False,
        input: str | None = None, cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        command = tuple(map(str, argv))
        assert input is not None
        if urlsplit(command[-2]).path == TURN_PATH:
            self.activity.append("turn")
            self.requests.append(json.loads(input))
            return self.responses.popleft()

        endpoint = urlsplit(command[-3])
        if endpoint.path == "/v1/chat/completions":
            prefix = tuple(stack.exec_argv(self.production_dir, GENERATOR.service_name))
            assert command[:len(prefix)] == prefix and endpoint.hostname == GENERATOR.service_name
            self.activity.append("grade")
            self.judge_requests.append(json.loads(input))
            return self.judge_responses.popleft()

        if "FROM public.chunks" in input:
            assert command == tuple(record.reader_argv(self.production_dir))
            read = "chunks"
            rows = {
                chunk_id: {"chunk_id": chunk_id, "doc_id": doc_id, "char_start": start, "char_end": end}
                for chunk_id, (doc_id, start, end) in self.chunks.items()
            }
        else:
            assert command == tuple(record.reader_argv(self.turns_dir))
            assert "FROM public.generation_documents" in input
            bound_gen = next(line for line in input.splitlines() if line.startswith("\\set v_name "))
            assert read_psql_set(bound_gen, "v_name") == self.generation.name
            if ":'v_source_ids'" in input:
                read = "gold sources"
                rows = {
                    source_id: {"source_id": source_id, "canonical_text_sha256": digest}
                    for source_id, digest in self.documents.values()
                }
            else:
                read = "pool documents"
                rows = {
                    doc_id: {"doc_id": doc_id, "source_id": source_id, "canonical_text_sha256": digest}
                    for doc_id, (source_id, digest) in self.documents.items()
                }
        variable = "v_source_ids" if read == "gold sources" else "v_ids"
        ids = tuple(
            identifier for line in input.splitlines() if line.startswith(f"\\set {variable} ")
            for identifier in json.loads(read_psql_set(line, variable))
        )
        self.reads.append((read, ids))
        if self.refused_read == read:
            return subprocess.CompletedProcess(command, 7, "", DIAGNOSTIC)
        stdout = "\n".join(json.dumps(rows[key]) for key in ids if key in rows)
        return subprocess.CompletedProcess(command, 0, stdout, "")


def _coordinate(index: int) -> Coordinates:
    return f"fictitious/source-{index}", sha256(PASSAGE.encode()).hexdigest(), 0, len(PASSAGE)


def _case(index: int, *, category: str = "retrieval") -> Case:
    source_id, digest, start, end = _coordinate(index)
    return {
        "id": f"research-qa-{category}-{index:03d}",
        "suite": "research-qa",
        "category": category,
        "question": f"{QUESTION} {index}",
        "gold": [{"source_id": source_id, "sha256": digest, "start": start, "end": end, "quote": PASSAGE}],
    }


def _set(
    *cases: Case, unsigned: frozenset[str] = frozenset(), signoffs: tuple[SignOff, ...] = (),
    slice_name: str = RETRIEVAL,
) -> LoadedSet:
    by_id = {cast(str, case["id"]): case for case in cases}
    ids = tuple(by_id)
    return LoadedSet(
        "eval-v-fictitious", {"fictitious.jsonl": cases}, by_id, ids,
        {slice_name: ids}, {slice_name: {"fictitious": ids}}, "fictitious-digest",
        unsigned_ids=unsigned, signoffs=signoffs,
    )


def _assert_no_text(test: unittest.TestCase, result: SliceResult, host: ResearchQAHost) -> None:
    surfaces = (result.report, *host.progress, repr(result), *(repr(row.metrics) for row in result.results))
    for sentinel in (QUESTION, PASSAGE, ANSWER, DIAGNOSTIC, REFERENCE, SIGNED_ANSWER, REASON):
        for rendered in surfaces:
            test.assertNotIn(sentinel, rendered)


class DecisionMetrics(unittest.TestCase):
    """Paired Research figures read from one case row's metrics."""

    def test_each_metric_reads_the_share_of_held_gold_in_evidence(self) -> None:
        for met, share in ((0, 0.0), (1, 0.25), (4, 1.0)):
            with self.subTest(met=met):
                row: dict[str, JSONValue] = {"held_gold_passages": 4, "met_in_evidence": met, "end": "complete"}
                self.assertEqual(research_qa_slice.IN_EVIDENCE.value(row), share)
                self.assertEqual(research_qa_slice.COMPLETE_IN_EVIDENCE.value(row), share)

    def test_both_metrics_skip_problems_unsigned_rows_and_cases_with_no_held_gold(self) -> None:
        excluded: dict[str, dict[str, JSONValue]] = {
            "problem": {"problem": "turn-failed"},
            "unsigned": {"unsigned": True},
            "no-held-gold": {"held_gold_passages": 0, "met_in_evidence": 0},
        }
        for reason, fields in excluded.items():
            for end in ("complete", "truncated"):
                with self.subTest(reason=reason, end=end):
                    row = {"held_gold_passages": 4, "met_in_evidence": 1, "end": end, **fields}
                    self.assertIsNone(research_qa_slice.IN_EVIDENCE.value(row))
                    self.assertIsNone(research_qa_slice.COMPLETE_IN_EVIDENCE.value(row))

    def test_only_synthesis_scores_incomplete_answers_as_zero(self) -> None:
        for end in ("truncated", "tripped", "abstained"):
            with self.subTest(end=end):
                row: dict[str, JSONValue] = {"held_gold_passages": 4, "met_in_evidence": 1, "end": end}
                self.assertEqual(research_qa_slice.IN_EVIDENCE.value(row), 0.25)
                self.assertEqual(research_qa_slice.COMPLETE_IN_EVIDENCE.value(row), 0.0)


class Retrieval(unittest.TestCase):
    """Retrieval reports gold coverage while the slice gates on turn validity."""

    def run_slice(self, loaded: LoadedSet, context: RunContext) -> SliceResult:
        return SLICE_RUNNERS[RETRIEVAL].runner(loaded, RETRIEVAL, context)

    def test_evidence_passes_and_pool_only_or_absent_gold_fails_the_row(self) -> None:
        cases = tuple(_case(index) for index in range(3))
        host = ResearchQAHost(baselines.BASELINES["off"])
        matching = host.add_chunk(_coordinate(0))
        pool_only = host.add_chunk(_coordinate(1))
        host.add_document(*_coordinate(2)[:2])
        decoy = host.add_chunk(_coordinate(3))
        host.queue_turn((decoy, matching), (matching, decoy))
        host.queue_turn((decoy, pool_only), (decoy,))
        host.queue_turn((decoy,), (decoy,))

        loaded = replace(_set(*reversed(cases)), held_out_ids=frozenset({cast(str, cases[2]["id"])}))
        result = self.run_slice(loaded, host.context())
        covered, missed, absent = result.results
        self.assertEqual(tuple(row.case_id for row in result.results), tuple(case["id"] for case in cases))
        self.assertTrue(result.verdict, result.report)
        self.assertIsNone(result.gated_cases)
        self.assertEqual((covered.verdict, missed.verdict, absent.verdict), ("pass", "fail", "fail"))
        self.assertEqual(covered.metrics, {
            "gold_passages": 1, "held_gold_passages": 1, "dropped": [], "least_rank": 2,
            "met_at_10": 1, "met_at_50": 1, "met_in_evidence": 1, "least_passage_number": 1,
            "pool_size": 2, "evidence_size": 2, "end": "complete", "attempts": 1,
        })
        self.assertEqual(missed.metrics["least_rank"], 2)
        self.assertEqual(missed.metrics["met_in_evidence"], 0)
        self.assertIsNone(missed.metrics["least_passage_number"])
        self.assertIsNone(absent.metrics["least_rank"])
        self.assertEqual(absent.metrics["held_gold_passages"], 1)
        self.assertEqual(tuple(row.latency_ms for row in result.results), (TOTAL_MS,) * len(cases))
        self.assertEqual([name for name, _ in host.reads], ["chunks", "pool documents", "gold sources"])
        self.assertCountEqual(host.reads[0][1], [decoy, matching, pool_only])
        self.assertEqual(host.reads[-1][1], tuple(_coordinate(index)[0] for index in range(len(cases))))
        for case, row, request in zip(cases, result.results, host.requests, strict=True):
            self.assertEqual(request["body"], {
                "message": case["question"], "overrides": host.overrides,
                "message_id": row.provenance_ref,
            })
        self.assertEqual(result.report.splitlines(), [
            f"{RETRIEVAL}: 3 counted, 2 tuning, 1 held-out, 0 unsigned excluded, "
            f"definition research-qa@1, generation {host.generation.name} ({host.generation.corpus_lockfile}), "
            f"door {TURN_PATH}, config hash {host.config_hash}, baseline off",
            "recall@10 2/3",
            "recall@50 2/3",
            "recall in evidence 1/3",
            "answer ends: complete 3, truncated 0, tripped 0, abstained 0",
            "failed turns: none",
            "unresolved pools: none",
            "dropped gold 0 (absent 0, changed 0)",
            "dropped cases: none",
            f"gold outside evidence: {missed.case_id} {absent.case_id}",
            f"gold outside pool: {absent.case_id}",
            f"{RETRIEVAL}: pass",
        ])
        self.assertEqual(host.progress[0], (
            f"research-qa {covered.case_id}: complete, pool 2, evidence 2, attempts 1, seconds {ELAPSED:.2f}"
        ))
        self.assertEqual(len(host.progress), len(cases))
        _assert_no_text(self, result, host)

    def test_recall_depths_include_rank_10_and_50(self) -> None:
        host = ResearchQAHost()
        edges = ((10, 1, 1), (11, 0, 1), (50, 0, 1), (51, 0, 0))
        cases = tuple(_case(rank) for rank, _, _ in edges)
        for rank, _, _ in edges:
            decoys = tuple(host.add_chunk(_coordinate(0)) for _ in range(rank - 1))
            gold = host.add_chunk(_coordinate(rank))
            host.queue_turn((*decoys, gold), (gold,))
        result = self.run_slice(_set(*cases), host.context())
        self.assertTrue(result.verdict, result.report)
        for row, (rank, at_10, at_50) in zip(result.results, edges, strict=True):
            with self.subTest(rank=rank):
                self.assertEqual(row.verdict, "pass")
                self.assertEqual(
                    (row.metrics["least_rank"], row.metrics["met_at_10"], row.metrics["met_at_50"]),
                    (rank, at_10, at_50),
                )
        self.assertIn("recall@10 1/4", result.report)
        self.assertIn("recall@50 3/4", result.report)

    def test_absent_and_changed_gold_are_dropped_from_recall(self) -> None:
        host = ResearchQAHost()
        absent, changed = _case(1), _case(2)
        decoy = host.add_chunk(_coordinate(3))
        changed_source, digest, start, end = _coordinate(2)
        changed_hash = sha256(b"Fictitious changed document").hexdigest()
        self.assertNotEqual(digest, changed_hash)
        changed_chunk = host.add_chunk((changed_source, changed_hash, start, end))
        host.queue_turn((decoy,), (decoy,))
        host.queue_turn((changed_chunk,), (changed_chunk,))
        result = self.run_slice(_set(absent, changed), host.context())
        self.assertTrue(result.verdict, result.report)
        for row, (source_id, kind) in zip(
            result.results, ((_coordinate(1)[0], "absent"), (changed_source, "changed")), strict=True,
        ):
            with self.subTest(kind=kind):
                self.assertEqual(row.verdict, "fail")
                self.assertEqual(row.metrics["held_gold_passages"], 0)
                self.assertEqual(row.metrics["dropped"], [{"source_id": source_id, "kind": kind}])
        self.assertIn("recall in evidence 0/0", result.report)
        self.assertIn("dropped gold 2 (absent 1, changed 1)", result.report)
        self.assertIn(f"dropped cases: {absent['id']} {changed['id']}", result.report)

    def test_truncated_and_tripped_answers_pass_with_evidence(self) -> None:
        host = ResearchQAHost()
        cases = (_case(1), _case(2))
        ends: tuple[events.EndWord, ...] = ("truncated", "tripped")
        for case, end in zip(cases, ends, strict=True):
            host.queue_gold_turn(case, end=end)
        result = self.run_slice(_set(*cases), host.context())
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(tuple((row.verdict, row.metrics["end"]) for row in result.results),
                         tuple(("pass", end) for end in ends))
        self.assertIn("answer ends: complete 0, truncated 1, tripped 1, abstained 0", result.report)
        _assert_no_text(self, result, host)

    def test_signed_harvest_uses_must_cite_at_document_level(self) -> None:
        host = ResearchQAHost()
        signed = {**_case(1), "id": "research-qa-001"}
        unsigned = {**_case(2), "id": "research-qa-002"}
        del signed["gold"], unsigned["gold"]
        source_id, digest, _, _ = _coordinate(3)
        signoff = SignOff("research-qa-001", ANSWER, (source_id,), "CSA-1", "2040-01-01")
        chunk = host.add_chunk((source_id, digest, 120, 180))
        host.queue_turn((chunk,), (chunk,))
        result = self.run_slice(
            _set(signed, unsigned, unsigned=frozenset({"research-qa-002"}), signoffs=(signoff,)),
            host.context(),
        )
        self.assertTrue(result.verdict, result.report)
        row, = result.results
        self.assertEqual((row.case_id, row.verdict), (signoff.case_id, "pass"))
        self.assertEqual(row.metrics["held_gold_passages"], 1)
        self.assertEqual(row.metrics["met_in_evidence"], 1)
        self.assertEqual(host.reads[-1], ("gold sources", signoff.must_cite))
        self.assertEqual(host.turn_questions(), [signed["question"]])
        self.assertIn("1 unsigned excluded", result.report)
        _assert_no_text(self, result, host)

    def test_case_without_gold_or_signoff_fails_its_row(self) -> None:
        host = ResearchQAHost()
        case = _case(1)
        del case["gold"]
        host.queue_turn(())
        result = self.run_slice(_set(case), host.context())
        self.assertTrue(result.verdict, result.report)
        row, = result.results
        self.assertEqual(row.verdict, "fail")
        self.assertEqual(row.metrics["gold_passages"], 0)
        self.assertIn(f"dropped cases: {row.case_id}", result.report)

    def test_missing_prerequisites_stop_before_any_turn(self) -> None:
        host = ResearchQAHost()
        context = host.context()
        case = _case(1)
        for code, loaded, supplied in (
            ("generation-unresolved", _set(case), replace(context, generation=None)),
            ("config-differs", _set(case), replace(context, expected_config_hash=None)),
            ("no-cases", _set(case, unsigned=frozenset({cast(str, case["id"])})), context),
        ):
            with self.subTest(code=code):
                result = self.run_slice(loaded, supplied)
                self.assertFalse(result.verdict)
                self.assertIsNone(result.gated_cases)
                if code == "no-cases":
                    self.assertEqual(result.results, ())
                else:
                    row, = result.results
                    self.assertEqual((row.verdict, row.metrics), ("fail", {"problem": code}))
                    self.assertIsNone(row.provenance_ref)
                self.assertIn(f"\n{code}: ", result.report)
                self.assertIn("\nFix: ", result.report)
                self.assertTrue(result.report.endswith(f"{RETRIEVAL}: fail\n"))
        self.assertEqual((host.requests, host.reads, host.activity), ([], [], []))

    def test_foreign_counted_or_unsigned_ids_stop_before_any_turn(self) -> None:
        host = ResearchQAHost()
        first, second = (_case(index, category="synthesis") for index in (1, 2))
        foreign_ids = tuple(cast(str, case["id"]) for case in (first, second))
        counted = _case(3)
        for unsigned, run_unsigned in (
            (frozenset(), False), (frozenset(foreign_ids), False), (frozenset(foreign_ids), True),
        ):
            with self.subTest(unsigned=unsigned, run_unsigned=run_unsigned):
                result = self.run_slice(
                    _set(second, counted, first, unsigned=unsigned), replace(host.context(), unsigned=run_unsigned),
                )
                self.assertFalse(result.verdict)
                self.assertEqual(
                    tuple(row.case_id for row in result.results),
                    tuple(sorted({cast(str, counted["id"]), *foreign_ids} - unsigned)),
                )
                self.assertTrue(all(row.metrics == {"problem": "case-outside-category"} for row in result.results))
                self.assertIn(f"selected cases outside research-qa/retrieval: {' '.join(foreign_ids)}", result.report)
                self.assertIn(f"Fix: List only research-qa/retrieval cases in the set's {RETRIEVAL} slice", result.report)
        self.assertEqual((host.requests, host.reads, host.activity), ([], [], []))

    def test_turn_failure_codes_fail_the_slice_without_record_reads(self) -> None:
        case = _case(1)
        for variant, code in (
            ("generation", "generation-differs"), ("lockfile", "generation-differs"),
            ("config", "config-differs"), ("failure", "turn-failed"),
            ("cut", "turn-cut"), ("malformed", "turn-reply-invalid"),
        ):
            with self.subTest(variant=variant):
                host = ResearchQAHost()
                match variant:
                    case "generation":
                        other = replace(host.generation, name=host.generation.name.replace(
                            host.generation.corpus_lockfile, "corpus-2040-02-03",
                        ))
                        self.assertNotEqual(other.name, host.generation.name)
                        host.queue_turn((), generation=other)
                    case "lockfile":
                        host.queue_turn((), generation=replace(host.generation, corpus_lockfile="corpus-2040-02-03"))
                    case "config":
                        host.queue_turn((), config_hash=sha256(b"Fictitious different config").hexdigest())
                    case "failure":
                        host.queue_failure(State.GENERATOR_UNAVAILABLE)
                    case "cut":
                        host.responses.append(subprocess.CompletedProcess(
                            ("fictitious",), doorclient.FAILURE_EXIT_CODE,
                            json.dumps({"kind": "failure", "exception": "TimeoutError"}), DIAGNOSTIC,
                        ))
                    case "malformed":
                        host.responses.append(stream_envelope((events.phase("generate", 1),), ELAPSED))
                result = self.run_slice(_set(case), host.context())
                self.assertFalse(result.verdict)
                self.assertIsNone(result.gated_cases)
                row, = result.results
                self.assertEqual(row.verdict, "fail")
                self.assertEqual(row.metrics["problem"], code)
                self.assertEqual(row.latency_ms, TOTAL_MS if variant in {"generation", "lockfile", "config"} else None)
                if variant == "failure":
                    self.assertEqual(row.metrics["turn_state"], State.GENERATOR_UNAVAILABLE.value)
                self.assertEqual(host.reads, [])
                self.assertIn(f"failed turns: {row.case_id}={code}", result.report)
                _assert_no_text(self, result, host)

    def test_unresolved_pool_fails_even_if_another_case_passes(self) -> None:
        host = ResearchQAHost()
        bad, good = _case(1), _case(2)
        missing = sha256(b"Fictitious missing chunk").hexdigest()
        host.queue_turn((missing,), (missing,))
        host.queue_gold_turn(good)
        result = self.run_slice(_set(bad, good), host.context())
        self.assertFalse(result.verdict)
        unresolved, covered = result.results
        self.assertEqual(unresolved.metrics, {"problem": "pool-unresolved"})
        self.assertEqual((unresolved.latency_ms, covered.verdict), (TOTAL_MS, "pass"))
        self.assertIn(f"unresolved pools: {unresolved.case_id}=pool-unresolved", result.report)

    def test_record_refusal_keeps_turn_failure_and_provenance(self) -> None:
        for read in ("chunks", "gold sources"):
            with self.subTest(read=read):
                host = ResearchQAHost()
                host.refused_read = read
                cases = (_case(1), _case(2))
                host.queue_failure(State.GENERATOR_UNAVAILABLE)
                host.queue_gold_turn(cases[1])
                result = self.run_slice(_set(*reversed(cases)), host.context())
                self.assertFalse(result.verdict)
                self.assertEqual(host.activity[:6], ["checkpoint", "mint", "turn"] * len(cases))
                failed, unread = result.results
                self.assertEqual(failed.metrics, {
                    "problem": "turn-failed", "turn_state": State.GENERATOR_UNAVAILABLE.value,
                })
                self.assertEqual(unread.metrics, {"problem": "record-unreachable"})
                self.assertEqual((failed.latency_ms, unread.latency_ms), (None, TOTAL_MS))
                self.assertEqual([row.provenance_ref for row in result.results], host.message_ids)
                self.assertEqual(len(set(host.message_ids)), len(cases))
                self.assertEqual(len(host.progress), len(cases))
                self.assertIn(f"{failed.case_id}: turn-failed", host.progress[0])
                for case, row, request in zip(cases, result.results, host.requests, strict=True):
                    message_id = row.provenance_ref
                    assert message_id is not None
                    self.assertEqual(bounded_id(message_id), message_id)
                    self.assertEqual(UUID(message_id).version, 4)
                    self.assertEqual(request["body"], {"message": case["question"], "message_id": message_id})
                    self.assertIn(f"provenance_ref={message_id!r}", repr(row))
                self.assertEqual(host.reads[0], ("chunks", tuple(host.chunks)))
                self.assertEqual(host.reads[-1][0], read)
                directory = host.production_dir if read == "chunks" else host.turns_dir
                self.assertEqual(result.report.count("\nFix: "), 1)
                self.assertIn(stack.logs_fix(directory, record.POSTGRES_SERVICE), result.report)
                self.assertIn(f"failed turns: {failed.case_id}=turn-failed", result.report)
                self.assertIn(f"record-unreachable cases: {unread.case_id}", result.report)
                _assert_no_text(self, result, host)

    def test_imports_are_standard_library_or_gideon(self) -> None:
        path = Path(cast(str, research_qa_slice.__file__))
        tree = ast.parse(path.read_bytes(), filename=str(path))
        for node in iter_imports(tree, module_level_only=False):
            for name in absolute_targets(node, path):
                with self.subTest(module=name, line=node.lineno):
                    root = name.partition(".")[0]
                    self.assertTrue(root == "gideon" or root in sys.stdlib_module_names, name)


def _synthesis_case(index: int) -> Case:
    return {**_case(index, category="synthesis"), "expected": {"answer": f"{REFERENCE} {index}"}}


class Synthesis(unittest.TestCase):
    """Synthesis grades complete turns and keeps its verdicts independent of grading."""

    def run_slice(self, loaded: LoadedSet, context: RunContext) -> SliceResult:
        return SLICE_RUNNERS[SYNTHESIS].runner(loaded, SYNTHESIS, context)

    def test_grading_uses_both_reference_sources_and_stays_in_the_judge_field(self) -> None:
        host = ResearchQAHost()
        synthetic = _synthesis_case(2)
        signed = {**_case(1, category="synthesis"), "id": "research-qa-001"}
        del signed["gold"]
        signoff = SignOff("research-qa-001", SIGNED_ANSWER, (_coordinate(1)[0],), "CSA-1", "2040-01-01")
        signed_chunk = host.add_chunk(_coordinate(1))
        host.queue_turn((signed_chunk,), (signed_chunk,))
        host.queue_gold_turn(synthetic)
        high = valid_content(score=3, reason=REASON)
        low = valid_content(score=0, reason=REASON, modes=("contradicts-reference",))
        for content in (high, low):
            host.queue_grade(engine_output(content, prompt_tokens=13, completion_tokens=5, elapsed=1.25))
        context = host.context(judge_prompt_id=PROMPT.id)
        result = self.run_slice(_set(synthetic, signed, signoffs=(signoff,), slice_name=SYNTHESIS), context)
        self.assertTrue(result.verdict, result.report)
        self.assertIsNone(result.gated_cases)
        self.assertEqual(host.activity[:10], ["checkpoint", "mint", "turn", "checkpoint", "grade"] * 2)
        references = (signoff.answer, cast(Mapping[str, str], synthetic["expected"])["answer"])
        for row, case, request, reference, content in zip(
            result.results, (signed, synthetic), host.judge_requests, references, (high, low), strict=True,
        ):
            with self.subTest(case_id=row.case_id):
                self.assertEqual(row.case_id, case["id"])
                self.assertEqual(row.verdict, "pass")
                self.assertEqual(row.latency_ms, TOTAL_MS)
                document = json.loads(content)
                self.assertEqual(row.judge, {"prompt": PROMPT.id, **document})
                self.assertTrue(row.metrics.keys().isdisjoint({"judge", "score", "reason", "failure_modes", "band"}))
                self.assertEqual(request["model"], context.served_model_name)
                self.assertEqual(request["messages"], [
                    {"role": "system", "content": PROMPT.system},
                    {"role": "user", "content": PROMPT.user_template.format(
                        question=case["question"], reference=reference, candidate=ANSWER,
                    )},
                ])
                digest = sha256(REASON.encode()).hexdigest()[:12]
                self.assertIn(
                    f"judge {row.case_id} repeat 1: {document['score']}; modes {len(document['failure_modes'])}; "
                    f"reason_length {len(REASON)}; reason_sha256 {digest}; "
                    "prompt_tokens 13; completion_tokens 5; seconds 1.25",
                    host.progress,
                )
        self.assertEqual(len(host.progress), 4)
        self.assertIn("graded 2: grade 0 1, grade 1 0, grade 2 0, grade 3 1", result.report.splitlines())
        self.assertIn("ungraded 0: truncated 0, tripped 0, abstained 0, problem 0, no-reference 0", result.report.splitlines())
        self.assertIn("failed gradings 0: none", result.report.splitlines())
        _assert_no_text(self, result, host)

    def test_failed_gradings_are_counted_by_code_and_schema_path_without_moving_verdicts(self) -> None:
        failures = (
            ("request-failed", "request-failed", "", 7),
            ("http-status", "http-status", engine_output(REASON, status=503), 0),
            ("finish-reason", "finish-reason", engine_output(None, finish_reason="length"), 0),
            ("no-content", "no-content", engine_output(None), 0),
            ("invalid-json", "invalid-json", engine_output(REASON), 0),
            ("schema-violation", "schema-violation /score", engine_output(valid_content(score=99, reason=REASON)), 0),
            ("schema-violation", "schema-violation /failure_modes/0",
             engine_output(valid_content(reason=REASON, modes=("fictitious-unknown-mode",))), 0),
            ("empty-reason", "empty-reason", engine_output(valid_content(reason=" \t ")), 0),
        )
        cases = tuple(_synthesis_case(index) for index in range(len(failures)))
        loaded = _set(*cases, slice_name=SYNTHESIS)
        results: list[SliceResult] = []
        for use_failures in (False, True):
            host = ResearchQAHost()
            context = host.context(judge_prompt_id=PROMPT.id)
            for index, (code, _, stdout, returncode) in enumerate(failures):
                gold = host.add_chunk(_coordinate(index))
                host.queue_turn((gold,), () if index == len(cases) - 1 else (gold,))
                if use_failures:
                    host.queue_grade(stdout, returncode=returncode)
                    if code == "finish-reason":
                        host.queue_grade(stdout)
                else:
                    host.queue_grade(engine_output(valid_content(reason=REASON)))
            result = self.run_slice(loaded, context)
            self.assertTrue(result.verdict, result.report)
            self.assertEqual(result.results[-1].verdict, "fail")
            _assert_no_text(self, result, host)
            if use_failures:
                assert context.judge_retry is not None
                retries = [request["seed"] for request in host.judge_requests if "seed" in request]
                self.assertEqual(retries, [context.judge_retry.seed])
                self.assertEqual(len(host.progress), 2 * len(cases))
            results.append(result)
        good, failed = results
        self.assertEqual(
            [(row.verdict, row.metrics) for row in good.results],
            [(row.verdict, row.metrics) for row in failed.results],
        )
        for row, (code, cause, _, _) in zip(failed.results, failures, strict=True):
            with self.subTest(cause=cause):
                self.assertIsNotNone(row.judge)
                assert row.judge is not None
                self.assertEqual(row.judge["failed"], code)
                self.assertNotIn("score", row.judge)
                if cause != code:
                    self.assertEqual(row.judge["detail"], f"schema violation at {cause.removeprefix(code + ' ')}")
        counts = ", ".join(f"{cause} 1" for cause in sorted(cause for _, cause, _, _ in failures))
        self.assertIn(f"failed gradings {len(failures)}: {counts}", failed.report.splitlines())
        self.assertIn("graded 0: grade 0 0, grade 1 0, grade 2 0, grade 3 0", failed.report.splitlines())
        self.assertIn("ungraded 0: truncated 0, tripped 0, abstained 0, problem 0, no-reference 0", failed.report.splitlines())

    def test_truncated_and_tripped_answers_fail_only_their_rows_and_are_ungraded(self) -> None:
        host = ResearchQAHost()
        cases = (_synthesis_case(1), _synthesis_case(2))
        ends: tuple[events.EndWord, ...] = ("truncated", "tripped")
        for case, end in zip(cases, ends, strict=True):
            host.queue_gold_turn(case, end=end)
        result = self.run_slice(_set(*cases, slice_name=SYNTHESIS), host.context(judge_prompt_id=PROMPT.id))
        self.assertTrue(result.verdict, result.report)
        self.assertIsNone(result.gated_cases)
        self.assertEqual(tuple((row.verdict, row.metrics["end"], row.judge) for row in result.results),
                         tuple(("fail", end, None) for end in ends))
        self.assertTrue(all(row.metrics["met_in_evidence"] == 1 for row in result.results))
        self.assertEqual(host.judge_requests, [])
        self.assertEqual(len(host.progress), len(cases))
        self.assertIn("ungraded 2: truncated 1, tripped 1, abstained 0, problem 0, no-reference 0", result.report.splitlines())
        _assert_no_text(self, result, host)

    def test_turn_problems_skip_grading_and_a_record_refusal_keeps_an_earlier_grade(self) -> None:
        host = ResearchQAHost()
        cases = tuple(_synthesis_case(index) for index in (1, 2, 3))
        host.queue_gold_turn(cases[0])
        host.queue_failure(State.GENERATOR_UNAVAILABLE)
        host.queue_turn((), config_hash=sha256(b"Fictitious different config").hexdigest())
        content = valid_content(reason=REASON)
        host.queue_grade(engine_output(content))
        host.refused_read = "chunks"
        result = self.run_slice(_set(*cases, slice_name=SYNTHESIS), host.context(judge_prompt_id=PROMPT.id))
        self.assertFalse(result.verdict)
        graded, failed, differed = result.results
        self.assertEqual(graded.metrics, {"problem": "record-unreachable"})
        self.assertEqual(graded.judge, {"prompt": PROMPT.id, **json.loads(content)})
        self.assertEqual((failed.metrics["problem"], differed.metrics["problem"]), ("turn-failed", "config-differs"))
        self.assertIsNone(failed.judge)
        self.assertIsNone(differed.judge)
        self.assertEqual(len(host.judge_requests), 1)
        self.assertIn("graded 1: grade 0 0, grade 1 0, grade 2 0, grade 3 1", result.report.splitlines())
        self.assertIn("ungraded 2: truncated 0, tripped 0, abstained 0, problem 2, no-reference 0", result.report.splitlines())
        _assert_no_text(self, result, host)

    def test_complete_turn_without_reference_is_reported_as_ungraded(self) -> None:
        host = ResearchQAHost()
        case = _case(1, category="synthesis")
        host.queue_gold_turn(case)
        result = self.run_slice(_set(case, slice_name=SYNTHESIS), host.context(judge_prompt_id=PROMPT.id))
        self.assertTrue(result.verdict, result.report)
        row, = result.results
        self.assertEqual(row.verdict, "pass")
        self.assertIsNone(row.judge)
        self.assertEqual(host.judge_requests, [])
        self.assertIn("ungraded 1: truncated 0, tripped 0, abstained 0, problem 0, no-reference 1", result.report.splitlines())


class CaseSelection(unittest.TestCase):
    """The runner's selection controls requests, measurements, and record rows."""

    def test_unsigned_turns_follow_counted_turns_with_their_own_verdicts(self) -> None:
        for slice_name in (RETRIEVAL, SYNTHESIS):
            with self.subTest(slice_name=slice_name):
                host = ResearchQAHost()
                category = slice_name.removeprefix("research-qa-")
                spec = SLICE_RUNNERS[slice_name]
                counted = _case(1) if slice_name == RETRIEVAL else _synthesis_case(1)
                first = {**_case(2, category=category), "id": "research-qa-001"}
                second = {**_case(3, category=category), "id": "research-qa-002"}
                del first["gold"], second["gold"]
                unsigned = frozenset({cast(str, first["id"]), cast(str, second["id"])})
                loaded = _set(second, first, counted, unsigned=unsigned, slice_name=slice_name)
                counted_chunk = host.add_chunk(_coordinate(1))
                unsigned_chunk = host.add_chunk(_coordinate(2))
                host.queue_turn((counted_chunk,), (counted_chunk,))
                host.queue_failure(State.GENERATOR_UNAVAILABLE)
                host.queue_turn((unsigned_chunk,), (unsigned_chunk,), end="tripped")
                if spec.judge_prompt is not None:
                    host.queue_grade(engine_output(valid_content(reason=REASON)))
                context = replace(
                    host.context(judge_prompt_id=spec.judge_prompt), unsigned=True, turn_generation=host.generation.name,
                )
                result = spec.runner(loaded, slice_name, context)

                self.assertTrue(result.verdict, result.report)
                self.assertIsNone(result.gated_cases)
                self.assertEqual(
                    host.turn_questions(),
                    [counted["question"], first["question"], second["question"]],
                )
                for request in host.requests:
                    self.assertEqual(cast(Mapping[str, object], request["body"]).get("generation"), host.generation.name)
                covered, failed, completed = result.results
                self.assertEqual((covered.case_id, failed.case_id, completed.case_id),
                                 (counted["id"], first["id"], second["id"]))
                self.assertEqual((covered.verdict, failed.verdict, completed.verdict), ("pass", "fail", "pass"))
                self.assertEqual(failed.metrics, {
                    "unsigned": True, "problem": "turn-failed", "turn_state": State.GENERATOR_UNAVAILABLE.value,
                })
                self.assertEqual(completed.metrics, {
                    "unsigned": True, "pool_size": 1, "evidence_size": 1, "end": "tripped", "attempts": 1,
                })
                self.assertIsNone(failed.judge)
                self.assertIsNone(completed.judge)
                self.assertEqual(len(host.judge_requests), int(spec.judge_prompt is not None))
                self.assertEqual([row.provenance_ref for row in result.results], host.message_ids)
                self.assertEqual((failed.latency_ms, completed.latency_ms), (None, TOTAL_MS))
                self.assertEqual(next(iter(failed.metrics)), "unsigned")
                self.assertEqual(next(iter(completed.metrics)), "unsigned")
                self.assertEqual(host.reads[0], ("chunks", (counted_chunk,)))
                self.assertEqual(host.reads[-1], ("gold sources", (_coordinate(1)[0],)))
                self.assertIn("2 unsigned run", result.report)
                self.assertIn("recall in evidence 1/1", result.report.splitlines())
                self.assertIn("answer ends: complete 1, truncated 0, tripped 0, abstained 0", result.report.splitlines())
                self.assertIn("failed turns: none", result.report.splitlines())
                self.assertIn("unsigned: completed 1, failed 1", result.report.splitlines())
                if spec.judge_prompt is not None:
                    self.assertIn("ungraded 0: truncated 0, tripped 0, abstained 0, problem 0, no-reference 0", result.report.splitlines())
                _assert_no_text(self, result, host)

    def test_pool_read_refusal_leaves_unsigned_row_untouched(self) -> None:
        host = ResearchQAHost()
        counted, unsigned = (_case(index) for index in (1, 2))
        host.queue_gold_turn(counted)
        pool = tuple(host.chunks)
        unsigned_chunk = host.add_chunk(_coordinate(2))
        host.queue_turn((unsigned_chunk,), (unsigned_chunk,))
        host.refused_read = "chunks"
        loaded = _set(counted, unsigned, unsigned=frozenset({cast(str, unsigned["id"])}))
        result = SLICE_RUNNERS[RETRIEVAL].runner(loaded, RETRIEVAL, replace(host.context(), unsigned=True))
        self.assertFalse(result.verdict)
        failed, completed = result.results
        self.assertEqual(failed.metrics, {"problem": "record-unreachable"})
        self.assertEqual(completed.verdict, "pass")
        self.assertEqual(completed.metrics, {
            "unsigned": True, "pool_size": 1, "evidence_size": 1, "end": "complete", "attempts": 1,
        })
        self.assertEqual((completed.provenance_ref, completed.latency_ms), (host.message_ids[1], TOTAL_MS))
        self.assertIsNone(completed.judge)
        self.assertEqual(host.reads, [("chunks", pool)])
        self.assertIn("unsigned: completed 1, failed 0", result.report.splitlines())

    def test_unsigned_turns_run_in_a_slice_with_no_counted_case(self) -> None:
        host = ResearchQAHost()
        cases = (_case(1), _case(2))
        for case in cases:
            host.queue_gold_turn(case)
        loaded = _set(*cases, unsigned=frozenset(cast(str, case["id"]) for case in cases))
        result = SLICE_RUNNERS[RETRIEVAL].runner(loaded, RETRIEVAL, replace(host.context(), unsigned=True))
        self.assertFalse(result.verdict)
        self.assertEqual(len(host.requests), len(cases))
        self.assertEqual(host.reads, [])
        self.assertEqual(
            tuple((row.case_id, row.verdict, row.metrics["unsigned"]) for row in result.results),
            tuple((case["id"], "pass", True) for case in cases),
        )
        self.assertEqual(tuple(row.provenance_ref for row in result.results), tuple(host.message_ids))
        lines = result.report.splitlines()
        self.assertTrue(lines[0].startswith(f"{RETRIEVAL}: 0 counted"), lines[0])
        self.assertIn("2 unsigned run", lines[0])
        self.assertIn("no-cases: the slice has no counted cases", lines)
        self.assertIn("unsigned: completed 2, failed 0", lines)
        self.assertEqual(lines[-1], f"{RETRIEVAL}: fail")

    def test_tuning_sends_only_tuning_questions_over_planted_and_committed_sets(self) -> None:
        first, held_out, last = (_case(index) for index in (1, 2, 3))
        planted = replace(_set(last, held_out, first), held_out_ids=frozenset({cast(str, held_out["id"])}))
        loaded = load_set(Path(__file__).resolve().parents[1] / SET_ROOT)
        self.assertTrue(loaded.ok, loaded.findings)
        assert loaded.loaded is not None
        for kind, eval_set, slice_name in (
            ("planted", planted, RETRIEVAL),
            ("committed", loaded.loaded, RETRIEVAL),
            ("committed", loaded.loaded, SYNTHESIS),
        ):
            with self.subTest(kind=kind, slice_name=slice_name):
                host = ResearchQAHost()
                spec = SLICE_RUNNERS[slice_name]
                selection = select_cases(eval_set, slice_name)
                self.assertTrue(selection.tuning)
                self.assertTrue(selection.held_out)
                tuning_ids = tuple(sorted(selection.tuning))
                for case_id in tuning_ids:
                    host.queue_gold_turn(eval_set.cases_by_id[case_id])
                    if spec.judge_prompt is not None:
                        host.queue_grade(engine_output(valid_content(reason=REASON)))
                context = replace(host.context(judge_prompt_id=spec.judge_prompt), tuning=True)
                result = spec.runner(eval_set, slice_name, context)
                self.assertTrue(result.verdict, result.report)
                self.assertEqual(tuple(row.case_id for row in result.results), tuning_ids)
                self.assertEqual(
                    host.turn_questions(),
                    [eval_set.cases_by_id[case_id]["question"] for case_id in tuning_ids],
                )
                self.assertTrue(result.report.startswith(f"{slice_name}: tuning run, {len(tuning_ids)} counted,"))
