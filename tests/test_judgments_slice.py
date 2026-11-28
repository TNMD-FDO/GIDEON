"""The judgments slice over ranked files and recorded Research replies."""

import ast
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from http import HTTPStatus
from itertools import product
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_evaluation_assessments import FIXTURE_MODEL, FIXTURE_PROMPT
from test_evaluation_record import read_psql_set
from test_evaluation_run import (
    EVALUATION,
    ROOT,
    EvalHost,
    _generation_row,
    _invoke,
    _run_kwargs,
)
from test_golddrop_slice import GoldDropHost, _context
from test_research_door import envelope, turn_pool_fixture, turn_pool_reply

from gideon.evaluation import baselines, record, research_door
from gideon.evaluation.assessments import Assessments
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set
from gideon.evaluation.generation import GenerationAccess
from gideon.evaluation.judgments import Judgment, path_for, serialize
from gideon.evaluation.judgments_slice import run_judgments
from gideon.evaluation.rankmetrics import (
    HOLE_DEPTH,
    RANKING_FLOOR,
    RELEVANT_GRADE,
    Coordinates,
)
from gideon.evaluation.rankmetrics2 import DEFINITION_ID
from gideon.evaluation.record import POSTGRES_SERVICE
from gideon.evaluation.results import CaseResult, JSONValue, RunContext, SliceResult
from gideon.host import models, report, secrets, site, stack
from gideon.host.index.sample import sample_name
from gideon.host.render.api import API_SERVICE_NAME
from gideon.host.render.ci import CI_ROOT, CI_STACK, PRODUCTION_STACK
from gideon.host.render.engine import RERANK
from gideon.turn.config import OverrideRefusal, config_hash, effective_config
from gideon.turn.states import State, TurnFailure

SENTINEL = "[FICTIONAL TEST ONLY] this question must never enter a stream"
FIGURE_KEYS = ("ndcg_at_10", "recall_at_50", "hole_at_10")


def _case(case_id: str) -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "judgments",
        "category": "judgments",
        "branch": "legal",
        "question": f"{SENTINEL} {case_id}",
        "labels": ["chu-written", "verbatim"],
        "cluster_id": case_id,
        "notes": "fixture note",
        "review": {"by": "CHU-attorney-1", "on": "2026-09-19", "accepted_flags": []},
    }


def _coordinate(index: int) -> tuple[str, str, int, int]:
    return (f"fictional/source-{index}", f"{index:064x}", 0, 4)


def _judgment(
    case_id: str,
    index: int,
    grade: int,
    *,
    assessment: str = "primary",
    grader: str = "CHU-attorney-1",
) -> Judgment:
    source_id, sha256, start, end = _coordinate(index)
    return Judgment(case_id, source_id, sha256, start, end, grade, grader, assessment)


def _write_jsonl(path: Path, values: Sequence[object]) -> bytes:
    data = "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _fixture(
    directory: str,
    grades: Mapping[int, int | None],
    *,
    second_grades: Mapping[int, int] | None = None,
) -> tuple[Path, tuple[str, ...]]:
    checkout = Path(directory) / "checkout"
    checkout.mkdir()
    shutil.copy(ROOT / "courts.yaml", checkout / "courts.yaml")
    set_root = checkout / "eval" / "sets" / "eval-v1"
    ids = tuple(f"judgments-{index:03d}" for index in sorted(grades))
    _write_jsonl(set_root / "judgments" / "queries.jsonl", [_case(case_id) for case_id in ids])
    slice_path = set_root / "slices" / "judgments" / "queries.ids"
    slice_path.parent.mkdir(parents=True, exist_ok=True)
    slice_path.write_text("".join(f"{case_id}\n" for case_id in ids), encoding="utf-8")
    judgment_values: list[str] = []
    for index in sorted(grades):
        grade = grades[index]
        if grade is not None:
            judgment_values.append(serialize(_judgment(f"judgments-{index:03d}", index, grade)))
        if second_grades is not None and index in second_grades:
            judgment_values.append(
                serialize(
                    _judgment(
                        f"judgments-{index:03d}",
                        index,
                        second_grades[index],
                        assessment="second",
                        grader="TRAD-attorney-1",
                    )
                )
            )
    if judgment_values:
        judgment_path = set_root / "judgments" / "judgments.jsonl"
        judgment_path.parent.mkdir(parents=True, exist_ok=True)
        judgment_path.write_text("".join(judgment_values), encoding="utf-8")
    return checkout, ids


def _ranked_file(directory: str, values: dict[int, tuple[int, ...]]) -> tuple[Path, bytes]:
    path = Path(directory) / "ranked.jsonl"
    rows = []
    for index in sorted(values):
        rows.append(
            {
                "query_id": f"judgments-{index:03d}",
                "ranked": [
                    {
                        "source_id": _coordinate(passage)[0],
                        "sha256": _coordinate(passage)[1],
                        "start": _coordinate(passage)[2],
                        "end": _coordinate(passage)[3],
                    }
                    for passage in values[index]
                ],
            }
        )
    data = _write_jsonl(path, rows)
    return path, data


def _write_ranked_bytes(directory: str, data: bytes) -> Path:
    path = Path(directory) / "ranked.jsonl"
    path.write_bytes(data)
    return path


def _assessed_file(
    directory: str, entries: Iterable[tuple[str, Coordinates, int]],
) -> tuple[Path, bytes]:
    """Write the file format independently of the assessments serializer."""

    path = Path(directory) / "assessments.jsonl"
    data = _write_jsonl(path, [
        {
            "query_id": case_id, "source_id": source_id, "sha256": sha256,
            "start": start, "end": end, "grade": grade,
            "prompt": FIXTURE_PROMPT, "model": FIXTURE_MODEL,
        }
        for case_id, (source_id, sha256, start, end), grade in entries
    ])
    return path, data


def _judgments_host(indices: Iterable[int]) -> GoldDropHost:
    host = GoldDropHost()
    host.generation_output = json.dumps(_generation_row()) + "\n"
    host.manifest = {source_id: sha256 for source_id, sha256, _, _ in map(_coordinate, indices)}
    return host


def _write_sql(host: EvalHost) -> str:
    writes = [
        input
        for argv, input in host.calls
        if argv[0] == "docker" and input is not None and "INSERT INTO eval_runs" in input
    ]
    if len(writes) != 1 or writes[0] is None:
        raise AssertionError(f"expected one SQL write, got {len(writes)}")
    return writes[0]


def _summary_line(stdout: str, prefix: str) -> str:
    """The one summary line starting with *prefix*, so a stray id is caught."""

    lines = [line for line in stdout.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, lines
    return lines[0]


def _binding(sql: str, variable: str) -> str:
    return read_psql_set(_summary_line(sql, f"\\set {variable} "), variable)


def _result_metrics(sql: str, index: int) -> dict[str, JSONValue]:
    return json.loads(_binding(sql, f"result_{index}_metrics"))


def _report_bytes(stdout: str) -> bytes:
    start = stdout.index("judged queries ")
    end = stdout.index("verdict ", start)
    return stdout[start:end].encode("utf-8")


class Command(unittest.TestCase):
    """The engine-free judgments runner follows the recorded command path."""

    def test_records_rows_figures_summary_and_overrides_without_case_text(self) -> None:
        grades = {1: 3, 2: 1, 3: 0, 4: 2, 5: None}
        with tempfile.TemporaryDirectory() as directory:
            checkout, ids = _fixture(directory, grades)
            ranked_path, ranked_bytes = _ranked_file(
                directory, {1: (1,), 2: (2,), 3: (3,), 4: (), 5: ()}
            )
            host = _judgments_host(index for index, grade in grades.items() if grade is not None)
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
            sql = _write_sql(host)
            loaded = load_set(checkout / SET_ROOT).loaded
            assert loaded is not None
            result = run_judgments(
                loaded, "judgments", replace(_context(host), ranked=dict.fromkeys(ids, ()))
            )
            self.assertIsNone(result.gated_cases)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn("INSERT", stdout)
        self.assertIn("ranked: ok", stdout)
        self.assertIn("5 queries, 3 passages", stdout)
        generation = _generation_row()
        self.assertEqual(
            _summary_line(stdout, "judged queries "),
            "judged queries 4 of 5, definition judgments@2, "
            f"generation {generation['gen_id']} ({generation['corpus_lockfile']}), door ranked-file",
        )
        self.assertIn("ndcg_at_10 mean 0.666667 count 3", stdout)
        self.assertIn("recall_at_50 mean 0.500000 count 2", stdout)
        self.assertIn("hole_at_10 mean 0.000000 count 3", stdout)
        self.assertEqual(
            _summary_line(stdout, "no relevant passage:"),
            "no relevant passage: judgments-002 judgments-003",
        )
        self.assertEqual(
            _summary_line(stdout, "no grade above 0:"), "no grade above 0: judgments-003"
        )
        # judgments-005 also has an empty list, but it is ungraded: it is left
        # out of Hole@10 for want of grades and belongs to the no-grades count.
        self.assertEqual(
            _summary_line(stdout, "empty ranked list:"), "empty ranked list: judgments-004"
        )
        self.assertIn("no grades yet: 1", stdout)
        self.assertIn(f"small set: 4 judged queries with held passages below {RANKING_FLOOR};", stdout)
        self.assertEqual(sql.count("INSERT INTO eval_runs"), 1)
        self.assertEqual(sql.count("INSERT INTO eval_results"), len(ids))
        result_verdicts = [
            line for line in sql.splitlines() if "_verdict 'pass'" in line
        ]
        self.assertEqual(len(result_verdicts), len(ids) + 1)
        metrics = [
            json.loads(read_psql_set(line, line.split()[1]))
            for line in sql.splitlines()
            if line.startswith("\\set result_") and "_metrics " in line
        ]
        self.assertEqual(
            [metric["ndcg_at_10"] for metric in metrics],
            [1.0, 1.0, None, 0.0, None],
        )
        self.assertEqual(
            [metric["recall_at_50"] for metric in metrics],
            [1.0, None, None, 0.0, None],
        )
        self.assertEqual(
            [metric["hole_at_10"] for metric in metrics],
            [0.0, 0.0, 0.0, None, None],
        )
        for variable, expected in (
            ("generation_id", generation["gen_id"]),
            ("corpus_lockfile", generation["corpus_lockfile"]),
        ):
            self.assertEqual(_binding(sql, variable), expected)
            self.assertIn(f":'{variable}'", sql)
        self.assertEqual(
            _binding(sql, "overrides"),
            json.dumps({
                "judgments": {
                    "definition": DEFINITION_ID,
                    "ranked_sha256": hashlib.sha256(ranked_bytes).hexdigest(),
                }
            }, sort_keys=True),
        )
        self.assertNotIn(SENTINEL, stdout + stderr + sql)

    def test_hand_written_assessments_add_only_judge_facts_and_two_report_lines(self) -> None:
        relevant = RELEVANT_GRADE
        source_id, sha256, start, end = _coordinate(1)
        overlap = (source_id, sha256, start + 1, end - 1)
        ranked = (
            overlap, _coordinate(1), _coordinate(7), _coordinate(8),
            *((source_id, sha256, start, end + step) for step in range(1, HOLE_DEPTH - 3)),
            _coordinate(9),
        )
        with tempfile.TemporaryDirectory() as directory:
            checkout, (held, dropped) = _fixture(
                directory, {1: relevant, 2: relevant + 1}, second_grades={1: 0},
            )
            court_path = checkout / "courts.yaml"
            ranked_path = Path(directory) / "ranked.jsonl"
            ranked_bytes = _write_jsonl(ranked_path, [
                {
                    "query_id": case_id,
                    "ranked": [
                        {"source_id": source, "sha256": digest, "start": lo, "end": hi}
                        for source, digest, lo, hi in entries
                    ],
                }
                for case_id, entries in ((held, ranked), (dropped, (_coordinate(7),)))
            ])
            assessed_path, assessed_bytes = _assessed_file(directory, (
                (held, _coordinate(1), relevant),
                (held, overlap, 0),
                (held, _coordinate(7), relevant),
                (held, _coordinate(9), relevant + 1),  # Below Hole@10.
                (dropped, _coordinate(2), relevant - 1),
            ))
            argv = ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)]
            baseline_host = _judgments_host((1, 7, 8, 9))
            base_code, base_stdout, base_stderr = _invoke(
                argv, **_run_kwargs(baseline_host, checkout=checkout, court_path=court_path),
            )
            host = _judgments_host((1, 7, 8, 9))
            code, stdout, stderr = _invoke(
                [*argv, "--assessed", str(assessed_path)],
                **_run_kwargs(host, checkout=checkout, court_path=court_path),
            )
            base_sql = _write_sql(baseline_host)
            sql = _write_sql(host)

        self.assertEqual((base_code, base_stderr), (0, ""), base_stdout)
        self.assertEqual((code, stderr), (0, ""), stdout)
        generation = _generation_row()
        hole_line = f"hole_at_10 mean {2 / HOLE_DEPTH:.6f} count 1\n"
        drop_source, drop_sha, drop_start, drop_end = _coordinate(2)
        legacy_report = (
            "judged queries 2 of 2, definition judgments@2, "
            f"generation {generation['gen_id']} ({generation['corpus_lockfile']}), door ranked-file\n"
            "ndcg_at_10 mean 1.000000 count 1\n"
            "recall_at_50 mean 1.000000 count 1\n"
            f"{hole_line}"
            "dropped passages 1 (absent 1, changed 0) across 1 queries\n"
            f"{dropped}: {drop_source}@{drop_sha}:{drop_start}-{drop_end}\n"
            f"dropped queries: {dropped}\n"
            "no grades yet: 0\n"
            f"small set: 1 judged queries with held passages below {RANKING_FLOOR}; "
            "the set detects regressions and ranks nothing\n"
        )
        self.assertEqual(_report_bytes(base_stdout), legacy_report.encode())
        # One exact pair agrees. The absent passage crosses the relevance line.
        # Exact chance agreement is 1/4, and collapsed chance agreement is 1/2.
        assessor_lines = (
            f"assessor {FIXTURE_PROMPT}, model {FIXTURE_MODEL}, "
            f"holes at ranks 1-{HOLE_DEPTH} 2 across 1 queries, assessed 1, unassessed 1, "
            "grade 0 0, grade 1 0, grade 2 1, grade 3 0, relevant 1\n"
            "assessor disagreement 2 pairs across 2 queries, four-grade differing 1 "
            "rate 0.500000 kappa 0.333333, relevant differing 1 rate 0.500000 kappa 0.000000\n"
        )
        assessed_report = legacy_report.replace(hole_line, hole_line + assessor_lines)
        self.assertEqual(_report_bytes(stdout), assessed_report.encode())
        self.assertNotIn("assessed:", base_stdout)
        self.assertNotIn("_judge ", base_sql)
        digest = hashlib.sha256(assessed_bytes).hexdigest()
        self.assertEqual(_summary_line(stdout, "assessed: ok"), (
            f"assessed: ok — 2 queries, 5 lines, prompt {FIXTURE_PROMPT}, "
            f"model {FIXTURE_MODEL}, SHA-256 {digest}"
        ))
        self.assertEqual(json.loads(_binding(sql, "overrides")), {"judgments": {
            "definition": "judgments@2",
            "ranked_sha256": hashlib.sha256(ranked_bytes).hexdigest(),
            "assessed_sha256": digest,
            "assessed_prompt": FIXTURE_PROMPT,
            "assessed_model": FIXTURE_MODEL,
        }})
        figures = (1.0, 1.0, round(2 / HOLE_DEPTH, 6))
        for index, case_id, expected_figures, hole_fields, differing in (
            (0, held, figures, (2, 1, 0, 0, 1, 0), 0),
            (1, dropped, (None, None, None), (None,) * 6, 1),
        ):
            self.assertEqual(_binding(sql, f"result_{index}_case_id"), case_id)
            self.assertEqual(_binding(sql, f"result_{index}_verdict"), "pass")
            metrics = _result_metrics(sql, index)
            self.assertEqual(metrics, _result_metrics(base_sql, index))
            self.assertEqual(tuple(metrics[key] for key in FIGURE_KEYS), expected_figures)
            judge = json.loads(_binding(sql, f"result_{index}_judge"))
            expected = {
                "prompt": FIXTURE_PROMPT, "model": FIXTURE_MODEL, "pair_count": 1,
                "four_grade_differing": differing, "relevant_differing": differing,
                **dict(zip(
                    ("hole_count", "assessed_hole_count", "grade_0", "grade_1", "grade_2", "grade_3"),
                    hole_fields, strict=True,
                )),
            }
            self.assertEqual(judge, expected)
            for key, value in expected.items():
                self.assertIs(type(judge[key]), type(value))
        self.assertNotIn(SENTINEL, base_stdout + base_stderr + base_sql + stdout + stderr + sql)

    def test_small_set_line_drops_at_the_ranking_floor(self) -> None:
        grades = dict.fromkeys(range(1, RANKING_FLOOR + 1), 2)
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, grades)
            ranked_path, _ranked_bytes = _ranked_file(
                directory, {index: (index,) for index in grades}
            )
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)],
                **_run_kwargs(
                    _judgments_host(grades), checkout=checkout, court_path=checkout / "courts.yaml"
                ),
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(f"judged queries {RANKING_FLOOR} of {RANKING_FLOOR}", stdout)
        self.assertNotIn("small set:", stdout)

    def test_missing_ranked_query_fails_while_an_explicit_empty_list_is_scored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, (missing, empty) = _fixture(directory, {1: 2, 2: 3})
            ranked_path, _ = _ranked_file(directory, {2: ()})
            host = _judgments_host((1, 2))
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
            sql = _write_sql(host)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("record: ok — run", stdout)
        for index, case_id, verdict, figures in (
            (0, missing, "fail", (None, None, None)),
            (1, empty, "pass", (0.0, 0.0, None)),
        ):
            self.assertEqual(_binding(sql, f"result_{index}_case_id"), case_id)
            self.assertEqual(_binding(sql, f"result_{index}_verdict"), verdict)
            metrics = _result_metrics(sql, index)
            self.assertEqual(tuple(metrics[key] for key in FIGURE_KEYS), figures)
        self.assertIn("gate: refuse", stdout)
        self.assertEqual(
            _summary_line(stdout, "judged and not ranked:"),
            f"judged and not ranked: {missing}",
        )
        self.assertEqual(_summary_line(stdout, "empty ranked list:"), f"empty ranked list: {empty}")
        self.assertEqual(_summary_line(stdout, "ndcg_at_10 mean"), "ndcg_at_10 mean 0.000000 count 1")
        self.assertEqual(_summary_line(stdout, "recall_at_50 mean"), "recall_at_50 mean 0.000000 count 1")
        self.assertEqual(_summary_line(stdout, "hole_at_10 mean"), "hole_at_10 mean null count 0")

    def test_drops_keep_each_span_and_read_the_full_source_id_union(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, (partial, wholly_dropped, grade_zero) = _fixture(directory, {1: 2, 2: 3, 3: 0})
            changed = replace(_judgment(partial, 4, 3), start=20, end=24)
            earlier = replace(changed, start=10, end=14)
            absent = _judgment(wholly_dropped, 2, 3)
            with path_for(checkout / SET_ROOT).open("a", encoding="utf-8") as handle:
                handle.write(serialize(changed) + serialize(earlier))
            ranked_path, _ = _ranked_file(directory, {1: (9, 1), 2: (9,), 3: (3,)})
            host = _judgments_host((1, 3, 4, 9))
            host.manifest[changed.source_id] = "e" * 64
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
            sql = _write_sql(host)

        self.assertEqual((code, stderr), (0, ""), stdout)
        manifest_reads = [sql for _, sql in host.calls if "FROM public.generation_documents" in (sql or "")]
        self.assertEqual(len(manifest_reads), 1)
        manifest_sql = manifest_reads[0]
        assert manifest_sql is not None
        self.assertEqual(
            json.loads(_binding(manifest_sql, "v_source_ids")),
            [_coordinate(index)[0] for index in (1, 2, 3, 4, 9)],
        )
        self.assertEqual(_binding(manifest_sql, "v_name"), _generation_row()["gen_id"])
        changed_spans = [
            f"{row.source_id}@{row.sha256}:{row.start}-{row.end}" for row in (earlier, changed)
        ]
        absent_span = f"{absent.source_id}@{absent.sha256}:{absent.start}-{absent.end}"
        for index, case_id, expected_dropped in (
            (0, partial, changed_spans),
            (1, wholly_dropped, [absent_span]),
            (2, grade_zero, []),
        ):
            with self.subTest(case_id=case_id):
                self.assertEqual(_binding(sql, f"result_{index}_case_id"), case_id)
                self.assertEqual(_binding(sql, f"result_{index}_verdict"), "pass")
                metrics = _result_metrics(sql, index)
                self.assertEqual(metrics["dropped"], expected_dropped)
                self.assertEqual(metrics["dropped_passages"], len(expected_dropped))
                self.assertEqual(metrics["foreign_ranked"], 0)
        partial_metrics = _result_metrics(sql, 0)
        self.assertEqual(partial_metrics["graded_passages"], 3)
        self.assertEqual(partial_metrics["relevant_passages"], 1)
        self.assertEqual(partial_metrics["recall_at_50"], 1.0)
        dropped_metrics = _result_metrics(sql, 1)
        self.assertEqual(dropped_metrics["graded_passages"], 1)
        self.assertEqual(dropped_metrics["relevant_passages"], 0)
        self.assertEqual(dropped_metrics["ranked_passages"], 1)
        self.assertTrue(all(dropped_metrics[key] is None for key in FIGURE_KEYS))

        self.assertEqual(_summary_line(stdout, "ndcg_at_10 mean"), "ndcg_at_10 mean 0.630930 count 1")
        self.assertEqual(_summary_line(stdout, "recall_at_50 mean"), "recall_at_50 mean 1.000000 count 1")
        self.assertEqual(_summary_line(stdout, "hole_at_10 mean"), "hole_at_10 mean 0.250000 count 2")
        lines = stdout.splitlines()
        means_end = lines.index(_summary_line(stdout, "hole_at_10 mean")) + 1
        self.assertEqual(lines[means_end:means_end + 4], [
            "dropped passages 3 (absent 1, changed 2) across 2 queries",
            f"{partial}: {' '.join(changed_spans)}",
            f"{wholly_dropped}: {absent_span}",
            f"dropped queries: {wholly_dropped}",
        ])
        for prefix in ("no relevant passage:", "no grade above 0:"):
            self.assertEqual(_summary_line(stdout, prefix), f"{prefix} {grade_zero}")
        self.assertIn(f"small set: 2 judged queries with held passages below {RANKING_FLOOR};", stdout)

    def test_foreign_entries_fail_covered_rows_and_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, ids = _fixture(directory, {1: 3, 2: None})
            ranked_path, _ = _ranked_file(directory, {1: (1, 9), 2: (9,)})
            host = _judgments_host((1,))
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
            sql = _write_sql(host)

        self.assertEqual((code, stderr), (1, ""), stdout)
        self.assertEqual(_binding(sql, "run_verdict"), "fail")
        for index, case_id in enumerate(ids):
            self.assertEqual(_binding(sql, f"result_{index}_case_id"), case_id)
            self.assertEqual(_binding(sql, f"result_{index}_verdict"), "fail")
            metrics = _result_metrics(sql, index)
            self.assertEqual(metrics["foreign_ranked"], 1)
            self.assertTrue(all(metrics[key] is None for key in FIGURE_KEYS))
        self.assertEqual(_summary_line(stdout, "verdict "), "verdict FAIL")
        self.assertEqual(
            _summary_line(stdout, "foreign ranked entries"),
            f"foreign ranked entries 2: {' '.join(ids)}",
        )
        self.assertEqual(
            _summary_line(stdout, "gate:"),
            "gate: refuse — one or more judged queries had a failed turn, an unresolved pool entry, "
            "a missing ranked list, or a ranked entry the generation does not hold Fix: Review the reported "
            "query ids and check the Research service logs for failed turns or unresolved pool entries. "
            "For a ranked file, add a list for each missing query id or use --generation <name> to score "
            "against the generation it was drawn from.",
        )

    def test_double_graded_passage_uses_primary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3}, second_grades={1: 0})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            host = _judgments_host((1,))
            code, stdout, _stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
            sql = _write_sql(host)
        self.assertEqual(code, 0)
        self.assertIn("graded_passages", sql)
        metrics_line = next(
            line for line in sql.splitlines() if line.startswith("\\set result_0_metrics ")
        )
        metrics = json.loads(read_psql_set(metrics_line, "result_0_metrics"))
        self.assertEqual(metrics["graded_passages"], 1)
        self.assertEqual(metrics["ndcg_at_10"], 1.0)
        self.assertIn("gate: ok", stdout)

    def test_preconditions_refusals_keep_the_ranked_file_placeholder(self) -> None:
        self.addCleanup(secrets.select_directory, secrets.current_directory())
        generation_name = _generation_row()["gen_id"]
        sibling = ("--stack", CI_STACK)
        retry = "eval run --slice judgments --ranked <file>"
        sibling_retry = f"{retry} --stack {CI_STACK}"
        failed_queue = EvalHost()
        failed_queue.queue_code = 23
        failed_read = EvalHost()
        failed_read.generation_code = 23
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ = _fixture(directory, {1: 2})
            ranked_path, _ = _ranked_file(directory, {1: (1,)})
            set_root = checkout / SET_ROOT
            cases = (
                ((), EvalHost(effective_uid=1), "root privileges are required", retry, ", then retry."),
                (
                    ("--set", str(set_root)), EvalHost(effective_uid=1), "root privileges are required",
                    "eval run --slice judgments --set <dir> --ranked <file>", ", then retry.",
                ),
                (sibling, EvalHost(), "models lock is missing", sibling_retry, " again."),
                (
                    (*sibling, "--generation", generation_name), EvalHost(),
                    f"generation {generation_name} is not recorded on the {CI_STACK} stack",
                    sibling_retry, " --generation <name>, then retry.",
                ),
                # A read failure's retry names the same generation, never the stack's default.
                (
                    (*sibling, "--generation", generation_name), failed_read,
                    "generation record read failed (exit 23)",
                    sibling_retry, f" --generation {generation_name} again.",
                ),
                (sibling, failed_queue, "index job query failed (exit 23)", sibling_retry, " again."),
            )
            for flags, host, problem, retry_path, ending in cases:
                with self.subTest(problem=problem):
                    code, stdout, stderr = _invoke(
                        ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path), *flags],
                        **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
                    )
                    self.assertEqual((code, stderr), (1, ""), stdout)
                    refusal = _summary_line(stdout, "preconditions: refuse")
                    detail, _, fix = refusal.partition(" Fix: ")
                    self.assertIn(problem, detail)
                    self.assertTrue(fix.endswith(report.command(retry_path) + ending), refusal)
                    self.assertNotIn(str(ranked_path), fix)
                    self.assertNotIn(str(set_root), fix)
                    self.assertLess(stdout.index("ranked: ok"), stdout.index(refusal))
                    self.assertNotIn("run: ok", stdout)
                    self.assertNotIn("record:", stdout)

    def test_stray_and_malformed_ranked_inputs_refuse_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 2})
            malformed = _write_ranked_bytes(directory, b"not-json\n")
            host = EvalHost()
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(malformed)],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
            self.assertEqual(code, 1)
            self.assertIn("ranked file refused", stdout)
            self.assertIn("Fix:", stdout)
            self.assertIn("line is not one JSON object", stderr)
            self.assertEqual([call for call in host.calls if call[0][0] == "docker"], [])

            host = EvalHost()
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction", "--ranked", str(malformed)],
                **_run_kwargs(host),
            )
            self.assertEqual(code, 1)
            self.assertEqual(stderr, "")
            self.assertIn("Remove --ranked", stdout)
            self.assertEqual([call for call in host.calls if call[0][0] == "docker"], [])

    def test_root_retry_keeps_assessed_placeholder_without_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, (case_id,) = _fixture(directory, {1: RELEVANT_GRADE})
            ranked_path, _ = _ranked_file(directory, {1: (1,)})
            assessed_path, _ = _assessed_file(directory, ((case_id, _coordinate(1), RELEVANT_GRADE),))
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path),
                 "--assessed", str(assessed_path)],
                **_run_kwargs(
                    EvalHost(effective_uid=1), checkout=checkout, court_path=checkout / "courts.yaml",
                ),
            )
        self.assertEqual((code, stderr), (1, ""), stdout)
        retry = report.command("eval run --slice judgments --ranked <file> --assessed <file>")
        self.assertEqual(_summary_line(stdout, "preconditions: refuse"), (
            f"preconditions: refuse — root privileges are required Fix: Run {retry}, then retry."
        ))
        self.assertNotIn(str(assessed_path), stdout + stderr)

    def test_set_argument_skips_recording(self) -> None:
        self.addCleanup(secrets.select_directory, secrets.current_directory())
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 2})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            host = _judgments_host((1,))
            code, stdout, stderr = _invoke(
                [
                    "eval",
                    "run",
                    "--slice",
                    "judgments",
                    "--set",
                    str(checkout / "eval" / "sets" / "eval-v1"),
                    "--ranked",
                    str(ranked_path),
                    "--stack",
                    CI_STACK,
                    "--generation",
                    _generation_row()["gen_id"],
                ],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        preconditions = _summary_line(stdout, "preconditions: ok")
        self.assertIn("set supplied, so no writer probe", preconditions)
        self.assertIn(f"generation {_generation_row()['gen_id']}", preconditions)
        self.assertLess(stdout.index("ranked: ok"), stdout.index(preconditions))
        self.assertIn("record: ok — skipped", stdout)
        reads = [sql for argv, sql in host.calls if argv[0] == "docker"]
        self.assertEqual(len(reads), 3)
        for sql, table in zip(reads, ("procrastinate_jobs", "index_generations", "generation_documents"), strict=True):
            self.assertIn(table, sql or "")


class RunnerRefusals(unittest.TestCase):
    """An unresolved generation or an unreadable manifest fails every query with its fix."""

    def test_each_unavailable_input_fails_every_counted_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, ids = _fixture(directory, {1: 2, 2: None})
            loaded = load_set(checkout / SET_ROOT).loaded
            assert loaded is not None
        host = _judgments_host((1,))
        context = replace(_context(host), ranked={ids[0]: (_coordinate(1),), ids[1]: ()})
        host.manifest_code = 23
        retry = report.command("eval run --slice judgments --ranked <file> --generation <name>")
        assessed_retry = report.command(
            "eval run --slice judgments --ranked <file> --assessed <file> --generation <name>"
        )
        assessments = Assessments({ids[0]: {_coordinate(1): 0}}, FIXTURE_PROMPT, FIXTURE_MODEL)
        manifest_failure = "generation manifest read failed (exit 23)"
        manifest_fix = f"Run {stack.logs_fix(context.turns_dir, POSTGRES_SERVICE)}, then retry."
        cases = (
            (
                replace(context, generation=None), "generation-unresolved",
                "the stores generation was not resolved",
                f"Run {retry} through the command's stores preconditions, then retry.", 0,
            ),
            (
                replace(context, generation=None, assessments=assessments), "generation-unresolved",
                "the stores generation was not resolved",
                f"Run {assessed_retry} through the command's stores preconditions, then retry.", 0,
            ),
            (context, "record-unreachable", manifest_failure, manifest_fix, 1),
            (
                replace(context, assessments=assessments), "record-unreachable",
                manifest_failure, manifest_fix, 1,
            ),
        )
        for run_context, problem, detail, fix, reads in cases:
            with self.subTest(problem=problem):
                host.calls.clear()
                result = run_judgments(loaded, "judgments", run_context)
                self.assertFalse(result.verdict)
                self.assertEqual(result.results, tuple(
                    CaseResult(case_id, 1, "fail", {"problem": problem}) for case_id in ids
                ))
                self.assertEqual(result.report.splitlines(), [
                    f"{len(ids)} counted, 0 unsigned excluded, door ranked-file",
                    f"{problem}: {detail}",
                    f"Fix: {fix}",
                    "judgments: fail",
                ])
                self.assertEqual(len(host.calls), reads)


class ResearchRunner(unittest.TestCase):
    """Use the recorded turn and coordinate replies through the runner and command."""

    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.checkout, self.ids = _fixture(directory, dict.fromkeys(range(1, 9)))
        loaded = load_set(self.checkout / SET_ROOT).loaded
        assert loaded is not None
        self.reply = turn_pool_reply()
        fused = sorted(self.reply["pool"], key=lambda entry: entry["pool_rank"])
        first, second, *tail = fused
        self.pool = [{**entry, "rank": rank} for rank, entry in enumerate((second, first, *tail), start=1)]
        self.reply["pool"] = self.pool[1:] + self.pool[:1]
        self.generation = GenerationAccess(
            self.reply["generation"]["gen_id"], self.reply["generation"]["lockfiles"][0], "live",
        )
        records = turn_pool_fixture("coordinates.json")
        self.chunks = {row["chunk_id"]: row for row in records["chunks"]}
        self.documents = {row["doc_id"]: row for row in records["manifest"]}
        self.ranked: tuple[Coordinates, ...] = tuple(
            (document["source_id"], document["canonical_text_sha256"], chunk["char_start"], chunk["char_end"])
            for entry in self.pool
            for chunk in (self.chunks[entry["chunk_id"]],)
            for document in (self.documents[chunk["doc_id"]],)
        )
        self.loaded = replace(loaded, judgments=tuple(
            Judgment(case_id, *self.ranked[index], RELEVANT_GRADE, "CHU-attorney-1", "primary")
            for index, case_id in enumerate(self.ids[:-1])
        ))
        self.host = GoldDropHost()
        self.host.manifest = {row["source_id"]: row["canonical_text_sha256"] for row in self.documents.values()}
        self.events: list[str] = []
        self.progress: list[str] = []
        self.context = replace(
            _context(self.host), generation=self.generation, progress=self.progress.append,
            checkpoint=lambda: self.events.append("checkpoint"),
            expected_config_hash=self.reply["retrieval_config_hash"],
        )

    @contextmanager
    def _serve(
        self, loaded: LoadedSet, context: RunContext,
        replies: Mapping[str, subprocess.CompletedProcess[str] | subprocess.SubprocessError],
        *, chunk_exit: int = 0, probe_reply: subprocess.CompletedProcess[str] | None = None,
    ) -> Iterator[None]:
        host_run = self.host.run

        def respond(
            argv: list[str], *, input: str | None = None, **kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            self.assertNotIn(SENTINEL, repr(argv))
            if argv[-2].endswith(research_door.TURN_PATH):
                self.assertIn(str(Path(context.turns_dir) / "compose.yaml"), argv)
                assert input is not None
                body = json.loads(input)["body"]
                question = body["message"]
                if question == research_door.PROBE_MESSAGE:
                    assert probe_reply is not None
                    self.assertEqual(body, {
                        "message": research_door.PROBE_MESSAGE, "stop_after": "retrieve",
                        "overrides": {research_door.PROBE_KEY: True},
                    })
                    self.events.append("probe")
                    return probe_reply
                case_id = question.removeprefix(f"{SENTINEL} ")
                self.assertEqual(question, loaded.cases_by_id[case_id]["question"])
                self.assertEqual(body.get("overrides"), context.turn_overrides)
                self.assertEqual(body.get("generation"), context.turn_generation)
                self.events.append(case_id)
                response = replies[case_id]
                if isinstance(response, subprocess.SubprocessError):
                    raise response
                return response

            self.assertNotIn(SENTINEL, input or "")
            if input is None or not any(table in input for table in ("FROM public.chunks", "FROM public.generation_documents")):
                return host_run(argv, input=input, **kwargs)
            batches = [
                json.loads(read_psql_set(line, "v_ids"))
                for line in input.splitlines() if line.startswith("\\set v_ids ")
            ]
            if not batches:
                self.assertIn("FROM public.generation_documents", input)
                self.assertEqual(argv, record.reader_argv(context.turns_dir))
                self.events.append("scoring manifest")
                source_ids = json.loads(_binding(input, "v_source_ids"))
                stdout = "\n".join(json.dumps({
                    "source_id": source_id, "canonical_text_sha256": self.host.manifest[source_id],
                }) for source_id in source_ids if source_id in self.host.manifest)
                return subprocess.CompletedProcess(argv, 0, stdout, SENTINEL)
            if "FROM public.chunks" in input:
                self.assertEqual(argv, record.reader_argv(context.production_dir))
                self.events.append("pool chunks")
                rows, returncode = self.chunks, chunk_exit
            else:
                self.assertIn("FROM public.generation_documents", input)
                self.assertEqual(argv, record.reader_argv(context.turns_dir))
                self.events.append("pool manifest")
                rows, returncode = self.documents, 0
            stdout = "\n".join(json.dumps(rows[key]) for batch in batches for key in batch if key in rows)
            return subprocess.CompletedProcess(argv, returncode, stdout, SENTINEL)

        with patch.object(self.host, "run", side_effect=respond):
            yield

    def _run(
        self, loaded: LoadedSet, context: RunContext,
        replies: Mapping[str, subprocess.CompletedProcess[str] | subprocess.SubprocessError],
        *, chunk_exit: int = 0,
    ) -> SliceResult:
        stdout, stderr = io.StringIO(), io.StringIO()
        with self._serve(loaded, context, replies, chunk_exit=chunk_exit), redirect_stdout(stdout), redirect_stderr(stderr):
            result = run_judgments(loaded, "judgments", context)
        for index, event in enumerate(self.events):
            if event in replies:
                self.assertGreater(index, 0)
                self.assertEqual(self.events[index - 1], "checkpoint")
        self.assertNotIn(SENTINEL, stdout.getvalue() + stderr.getvalue() + "\n".join(self.progress))
        self.assertNotIn(SENTINEL, result.report)
        for row in result.results:
            self.assertNotIn(SENTINEL, repr(row))
            self.assertNotIn(SENTINEL, json.dumps(asdict(row)))
        return result

    def _command(
        self, probe_reply: subprocess.CompletedProcess[str],
        replies: Mapping[str, subprocess.CompletedProcess[str]], *, stack_name: str = PRODUCTION_STACK,
        baseline: str | None = None,
    ) -> tuple[int, str, str]:
        kwargs = _run_kwargs(self.host, checkout=self.checkout, court_path=self.checkout / "courts.yaml")
        kwargs["models_path"] = ROOT / "models.lock"
        sibling = stack_name == CI_STACK
        name = sample_name(self.generation.name) if sibling else self.generation.name
        self.host.generation_output = json.dumps({
            "gen_id": name, "corpus_lockfile": self.generation.corpus_lockfile, "state": self.generation.state,
        }) + "\n"
        context = replace(
            self.context, production_dir=kwargs["rendered_dir"],
            turns_dir=CI_ROOT if sibling else kwargs["rendered_dir"],
            turn_overrides=None if baseline is None else baselines.BASELINES[baseline],
            turn_generation=name,
        )
        argv = ["eval", "run", "--slice", "judgments", "--stack", stack_name, "--generation", name]
        if baseline is not None:
            argv.extend(("--baseline", baseline))
        with self._serve(self.loaded, context, replies, probe_reply=probe_reply), patch.object(secrets, "select_directory"):
            return _invoke(argv, **kwargs)

    def test_command_records_research_rows_and_overrides_without_a_ranked_stage(self) -> None:
        path_for(self.checkout / SET_ROOT).write_text(
            "".join(serialize(judgment) for judgment in self.loaded.judgments), encoding="utf-8",
        )
        refused = TurnFailure(State.OVERRIDE_NOT_ALLOWED, "overrides")
        probe_reply = envelope(refused.to_body(), refused.status)
        lock = models.load_models_lock(ROOT / "models.lock").lock
        site_config = site.load_site(ROOT / "config/site.example.yaml").config
        assert lock is not None and site_config is not None
        profile = lock.profile(site_config.hardware_profile)
        assert profile is not None
        for stack_name, baseline in product((PRODUCTION_STACK, CI_STACK), (None, *baselines.BASELINES)):
            with self.subTest(stack=stack_name, baseline=baseline):
                self.host.calls.clear()
                self.events.clear()
                reranker = stack_name == PRODUCTION_STACK and profile.model(RERANK.role) is not None
                name = sample_name(self.generation.name) if stack_name == CI_STACK else self.generation.name
                overrides = None if baseline is None else baselines.BASELINES[baseline]
                effective = effective_config(overrides, reranker=reranker)
                assert not isinstance(effective, OverrideRefusal)
                expected_hash = config_hash(effective)
                wire_reply = turn_pool_reply(overrides, reranker=reranker)
                if effective["rerank.stage2"] == "on":
                    wire_reply["pool"] = self.reply["pool"]
                wire_reply["generation"]["gen_id"] = name
                additional: dict[str, object] = {}
                if overrides is not None:
                    additional = {"baseline": baseline, "turn_overrides": dict(overrides)}
                reply = envelope(wire_reply, HTTPStatus.OK)
                with patch.object(baselines, "expected_config_hash", wraps=baselines.expected_config_hash) as prepare_hash:
                    code, stdout, stderr = self._command(
                        probe_reply, dict.fromkeys(self.ids, reply), stack_name=stack_name, baseline=baseline,
                    )
                prepare_hash.assert_called_once_with(overrides, reranker=reranker)
                self.assertEqual((code, stderr), (0, ""), stdout)
                self.assertEqual(self.events, ["probe", *self.ids, "pool chunks", "pool manifest", "scoring manifest"])
                self.assertEqual(
                    [line.split(":", 1)[0] for line in stdout.splitlines() if ": ok — " in line],
                    ["load", "preconditions", "run", "record", "gate"],
                )
                self.assertNotIn("ranked:", stdout)
                self.assertIn(
                    f"door {research_door.TURN_PATH} probed, baseline {baseline or 'shipped'}",
                    _summary_line(stdout, "preconditions: ok"),
                )
                self.assertIn(
                    f"config hash {expected_hash}, baseline {baseline or 'shipped'}",
                    _summary_line(stdout, "judged queries "),
                )
                # The turn door takes no engine lock.
                fake = self.host
                self.assertEqual(fake.lock_records, [])

                sql = _write_sql(self.host)
                self.assertEqual(sql.count("INSERT INTO eval_runs"), 1)
                self.assertEqual(sql.count("INSERT INTO eval_results"), len(self.ids))
                self.assertEqual(_binding(sql, "generation_id"), name)
                self.assertEqual(_binding(sql, "corpus_lockfile"), self.generation.corpus_lockfile)
                self.assertEqual(json.loads(_binding(sql, "overrides")), {"judgments": {
                    "definition": DEFINITION_ID, "door": "turn", "config_hash": expected_hash,
                    **additional,
                }})
                for index, case_id in enumerate(self.ids):
                    self.assertEqual(_binding(sql, f"result_{index}_case_id"), case_id)
                    self.assertEqual(_binding(sql, f"result_{index}_verdict"), "pass")
                    self.assertEqual(float(_binding(sql, f"result_{index}_latency_ms")), self.reply["timings_ms"]["total"])
                    self.assertEqual(_result_metrics(sql, index)["ranked_passages"], len(self.pool))
                self.assertNotIn(SENTINEL, stdout + stderr + sql)

                if baseline is None:
                    self.host.calls.clear()
                    self.events.clear()
                    wrong_hash = hashlib.sha256(b"Fictitious mismatched retrieval config").hexdigest()
                    replies = dict.fromkeys(self.ids, reply)
                    replies[self.ids[-1]] = envelope({**wire_reply, "retrieval_config_hash": wrong_hash}, HTTPStatus.OK)
                    with patch.object(baselines, "expected_config_hash", wraps=baselines.expected_config_hash) as prepare_hash:
                        code, stdout, stderr = self._command(probe_reply, replies, stack_name=stack_name)
                    prepare_hash.assert_called_once_with(None, reranker=reranker)
                    self.assertEqual((code, stderr), (1, ""), stdout)
                    self.assertEqual(_summary_line(stdout, "failed turns:"), f"failed turns: {self.ids[-1]}")
                    sql = _write_sql(self.host)
                    self.assertEqual(_result_metrics(sql, len(self.ids) - 1)["problem"], "config-differs")

    def test_command_routes_refused_probes_to_the_sibling_and_prints_their_fixes(self) -> None:
        for status, fix in (
            (HTTPStatus.NOT_FOUND, research_door.SIBLING_FIX_COMMAND),
            (HTTPStatus.UNAUTHORIZED, research_door.SIBLING_FIX_COMMAND),
            (HTTPStatus.FORBIDDEN, research_door.SIBLING_FIX_COMMAND),
            (HTTPStatus.INTERNAL_SERVER_ERROR, stack.logs_fix(CI_ROOT, API_SERVICE_NAME)),
        ):
            with self.subTest(status=status):
                self.events.clear()
                self.host.calls.clear()
                code, stdout, stderr = self._command(envelope(SENTINEL, status), {}, stack_name=CI_STACK)
                self.assertEqual((code, stderr), (1, ""), stdout)
                self.assertEqual(self.events, ["probe"])
                self.assertEqual([line.split(":", 1)[0] for line in stdout.splitlines()], ["load", "preconditions"])
                self.assertIn(fix, _summary_line(stdout, "preconditions: refuse"))
                self.assertNotIn(SENTINEL, stdout)
                self.assertFalse(any(argv[0] == "git" for argv, _ in self.host.calls))

    def test_turn_and_file_share_figures_and_assessments_in_rank_order(self) -> None:
        first, second, unsigned = self.ids[:3]
        loaded = replace(
            self.loaded, slices={"judgments": (unsigned, second, first)}, unsigned_ids=frozenset({unsigned}),
        )
        context = replace(
            self.context, turn_generation=self.generation.name,
            assessments=Assessments(
                {first: {self.ranked[0]: RELEVANT_GRADE, self.ranked[1]: 0}}, FIXTURE_PROMPT, FIXTURE_MODEL,
            ),
        )
        file_run = self._run(loaded, replace(context, ranked={first: self.ranked, second: ()}), {})
        self.assertTrue(file_run.verdict, file_run.report)
        self.assertTrue(file_run.report.splitlines()[0].endswith("door ranked-file"))
        self.assertTrue(any(row.metrics["ndcg_at_10"] for row in file_run.results))
        empty = {**self.reply, "pool": []}
        options: tuple[tuple[str, Mapping[str, JSONValue] | None], ...] = (
            ("shipped", None), *((word, dict(overrides)) for word, overrides in baselines.BASELINES.items()),
        )
        for baseline, overrides in options:
            with self.subTest(baseline=baseline):
                self.events.clear()
                expected_hash = baselines.expected_config_hash(overrides, reranker=True)
                assert isinstance(expected_hash, str)
                replies = {
                    case_id: envelope({**body, "retrieval_config_hash": expected_hash}, HTTPStatus.OK)
                    for case_id, body in ((first, self.reply), (second, empty))
                }
                run = self._run(loaded, replace(
                    context, turn_overrides=overrides, expected_config_hash=expected_hash,
                ), replies)
                self.assertTrue(run.verdict, run.report)
                self.assertEqual([event for event in self.events if event != "checkpoint"], [
                    first, second, "pool chunks", "pool manifest", "scoring manifest",
                ])
                self.assertEqual(run.results, tuple(
                    replace(row, latency_ms=self.reply["timings_ms"]["total"]) for row in file_run.results
                ))
                header, *lines = run.report.splitlines()
                self.assertEqual(header, file_run.report.splitlines()[0].replace(
                    "door ranked-file", f"door {research_door.TURN_PATH}, config hash {expected_hash}, baseline {baseline}",
                ))
                self.assertIn("failed turns: none", lines)
                self.assertIn("unresolved pools: none", lines)
                self.assertEqual(
                    [line for line in lines if line not in ("failed turns: none", "unresolved pools: none")],
                    file_run.report.splitlines()[1:],
                )

    def test_turn_refusals_fail_the_named_queries_without_figures(self) -> None:
        good, failed, cut, invalid, generation, lockfile, config, unresolved = self.ids
        del self.chunks[self.pool[-1]["chunk_id"]]
        reply = self.reply
        refused = TurnFailure(State.OVERRIDE_INVALID, "overrides")
        error = refused.to_body()
        error["error"]["message"] = SENTINEL
        replies: dict[str, subprocess.CompletedProcess[str] | subprocess.SubprocessError] = {
            good: envelope({**reply, "pool": self.pool[:-1]}, HTTPStatus.OK),
            failed: envelope(error, refused.status),
            cut: subprocess.TimeoutExpired("fixture", research_door.TURN_TIMEOUT_SECONDS, output=SENTINEL),
            invalid: envelope({**reply, "stage": "generate"}, HTTPStatus.OK),
            generation: envelope({**reply, "generation": {
                **reply["generation"], "gen_id": sample_name(self.generation.name),
            }}, HTTPStatus.OK),
            lockfile: envelope({**reply, "generation": {
                **reply["generation"], "lockfiles": ["corpus-2099-01-01"],
            }}, HTTPStatus.OK),
            config: envelope({**reply, "retrieval_config_hash": hashlib.sha256(b"Fictitious config").hexdigest()}, HTTPStatus.OK),
            unresolved: envelope(reply, HTTPStatus.OK),
        }
        run = self._run(self.loaded, self.context, replies)
        self.assertFalse(run.verdict)
        self.assertEqual(tuple(row.case_id for row in run.results), self.ids)
        passing, *failures = run.results
        self.assertEqual(passing.verdict, "pass", run.report)
        self.assertEqual(passing.latency_ms, self.reply["timings_ms"]["total"])
        self.assertEqual(tuple(row.metrics["problem"] for row in failures), (
            "turn-failed", "turn-cut", "turn-reply-invalid", "generation-differs",
            "generation-differs", "config-differs", "pool-unresolved",
        ))
        for row in failures:
            with self.subTest(case_id=row.case_id):
                self.assertEqual(row.verdict, "fail")
                self.assertTrue(all(row.metrics[key] is None for key in FIGURE_KEYS))
                expected_latency = None if row.case_id in (failed, cut, invalid) else self.reply["timings_ms"]["total"]
                self.assertEqual(row.latency_ms, expected_latency)
                if row.case_id == failed:
                    self.assertEqual(row.metrics["turn_state"], refused.state.value)
                else:
                    self.assertNotIn("turn_state", row.metrics)
        self.assertEqual(_summary_line(run.report, "failed turns:"), f"failed turns: {' '.join(self.ids[1:-1])}")
        self.assertEqual(_summary_line(run.report, "unresolved pools:"), f"unresolved pools: {unresolved}")

    def test_failed_coordinate_read_fails_every_counted_query_with_its_fix(self) -> None:
        reply = envelope(self.reply, HTTPStatus.OK)
        exit_code = 23
        run = self._run(self.loaded, self.context, dict.fromkeys(self.ids, reply), chunk_exit=exit_code)
        self.assertFalse(run.verdict)
        self.assertEqual(run.results, tuple(
            CaseResult(case_id, 1, "fail", {"problem": "record-unreachable"}, latency_ms=self.reply["timings_ms"]["total"])
            for case_id in self.ids
        ))
        self.assertIn(f"pool chunk read failed (exit {exit_code})", run.report)
        self.assertIn(stack.logs_fix(self.context.production_dir, POSTGRES_SERVICE), run.report)
        self.assertEqual([event for event in self.events if event != "checkpoint"], [*self.ids, "pool chunks"])

    def test_a_counted_case_of_another_suite_stops_the_run_before_any_turn(self) -> None:
        stray = self.ids[2]
        cases = dict(self.loaded.cases_by_id)
        cases[stray] = {"id": stray, "suite": "segmenter", "category": "segmenter"}
        run = self._run(replace(self.loaded, cases_by_id=cases), self.context, {})
        self.assertFalse(run.verdict)
        self.assertTrue(all(row.metrics == {"problem": "case-not-judgments"} for row in run.results))
        self.assertIn(f"counted cases outside the judgments queries: {stray}", run.report)
        self.assertIn("List only judgments queries in the set's judgments slice", run.report)
        self.assertEqual([event for event in self.events if event != "checkpoint"], [])

    def test_turn_retry_for_unresolved_generation_names_only_the_applicable_flags(self) -> None:
        context = replace(self.context, generation=None)
        run = self._run(self.loaded, context, {})
        retry = report.command("eval run --slice judgments --generation <name>")
        self.assertIn(retry, run.report)
        self.assertNotIn("--ranked", run.report)
        self.assertTrue(all(row.metrics == {"problem": "generation-unresolved"} for row in run.results))
        self.assertEqual(self.events, [])


class NoText(unittest.TestCase):
    """The Research door is the only consumer of a judgments question."""

    def test_new_modules_do_not_subscript_or_get_content_fields(self) -> None:
        forbidden = {"question", "notes", "labels", "text", "caption", "quote"}
        for name in (
            "assessments.py", "judgments_slice.py", "ranked.py", "rankmetrics.py", "rankmetrics2.py",
        ):
            path = EVALUATION / name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            question_reads = 0
            for node in ast.walk(tree):
                if isinstance(node, ast.Subscript):
                    key = node.slice.value if isinstance(node.slice, ast.Constant) else None
                    if name == "judgments_slice.py" and key == "question":
                        question_reads += 1
                        cast_call = parents[node]
                        assert isinstance(cast_call, ast.Call)
                        self.assertEqual(ast.unparse(cast_call.func), "cast")
                        self.assertIs(cast_call.args[1], node)
                        send_call = parents[cast_call]
                        assert isinstance(send_call, ast.Call)
                        self.assertEqual(ast.unparse(send_call.func), "research_door.send")
                        self.assertIs(send_call.args[2], cast_call)
                        continue
                    self.assertNotIn(key, forbidden, f"{path}: forbidden subscript {key!r}")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                ):
                    key = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
                    self.assertNotIn(key, forbidden, f"{path}: forbidden get {key!r}")
            self.assertEqual(question_reads, int(name == "judgments_slice.py"))


if __name__ == "__main__":
    unittest.main()
