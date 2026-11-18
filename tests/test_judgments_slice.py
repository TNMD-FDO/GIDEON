"""The judgments slice command over a content-free ranked-list fixture."""

import ast
import hashlib
import json
import shutil
import tempfile
import unittest
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

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

from gideon.evaluation.evalset import SET_ROOT, load_set
from gideon.evaluation.judgments import Judgment, path_for, serialize
from gideon.evaluation.judgments_slice import run_judgments
from gideon.evaluation.rankmetrics import RANKING_FLOOR
from gideon.evaluation.record import POSTGRES_SERVICE
from gideon.evaluation.results import CaseResult, JSONValue
from gideon.host import report, secrets, stack
from gideon.host.render.ci import CI_STACK

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
            f"generation {generation['gen_id']} ({generation['corpus_lockfile']})",
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
            json.loads(_binding(sql, "overrides")),
            {
                "judgments": {
                    "definition": "judgments@2",
                    "ranked_sha256": hashlib.sha256(ranked_bytes).hexdigest(),
                }
            },
        )
        self.assertNotIn(SENTINEL, stdout + stderr + sql)

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
            "gate: refuse — one or more judged queries have no ranked list or the file has ranked entries "
            "the generation does not hold Fix: Add a ranked list for each missing query id in the report, "
            "or use --generation <name> to score the file against the generation it was drawn from.",
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

    def test_missing_stray_and_malformed_ranked_inputs_refuse_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 2})
            malformed = _write_ranked_bytes(directory, b"not-json\n")
            cases = (
                (["eval", "run", "--slice", "judgments"], checkout, "--ranked", False),
                (
                    ["eval", "run", "--slice", "judgments", "--ranked", str(malformed)],
                    checkout,
                    "ranked file refused",
                    True,
                ),
            )
            for argv, case_checkout, expected, malformed_input in cases:
                with self.subTest(argv=argv):
                    host = EvalHost()
                    code, stdout, stderr = _invoke(
                        argv,
                        **_run_kwargs(
                            host,
                            checkout=case_checkout,
                            court_path=case_checkout / "courts.yaml",
                        ),
                    )
                    self.assertEqual(code, 1)
                    self.assertIn(expected, stdout)
                    self.assertIn("Fix:", stdout)
                    self.assertEqual(
                        [call for call in host.calls if call[0][0] == "docker"], []
                    )
                    if malformed_input:
                        self.assertIn("line is not one JSON object", stderr)
                    else:
                        retry = report.command("eval run --slice judgments --ranked <file>")
                        self.assertIn(f"Fix: Run {retry}, then retry.", stdout)

            host = EvalHost()
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction", "--ranked", str(malformed)],
                **_run_kwargs(host),
            )
            self.assertEqual(code, 1)
            self.assertEqual(stderr, "")
            self.assertIn("Remove --ranked", stdout)
            self.assertEqual([call for call in host.calls if call[0][0] == "docker"], [])

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
        cases = (
            (
                replace(context, generation=None), "generation-unresolved",
                "the stores generation was not resolved",
                f"Run {retry} through the command's stores preconditions, then retry.", 0,
            ),
            (
                context, "record-unreachable", "generation manifest read failed (exit 23)",
                f"Run {stack.logs_fix(context.turns_dir, POSTGRES_SERVICE)}, then retry.", 1,
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
                    f"{len(ids)} counted, 0 unsigned excluded",
                    f"{problem}: {detail}",
                    f"Fix: {fix}",
                    "judgments: fail",
                ])
                self.assertEqual(len(host.calls), reads)


class NoText(unittest.TestCase):
    """The runner modules use ids and coordinates, never case prose fields."""

    def test_new_modules_do_not_subscript_or_get_content_fields(self) -> None:
        forbidden = {"question", "notes", "labels", "text", "caption", "quote"}
        for name in ("judgments_slice.py", "ranked.py", "rankmetrics.py", "rankmetrics2.py"):
            path = EVALUATION / name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Subscript):
                    key = node.slice.value if isinstance(node.slice, ast.Constant) else None
                    self.assertNotIn(key, forbidden, f"{path}: forbidden subscript {key!r}")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                ):
                    key = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
                    self.assertNotIn(key, forbidden, f"{path}: forbidden get {key!r}")


if __name__ == "__main__":
    unittest.main()
