"""The judgments slice command over a content-free ranked-list fixture."""

import ast
import hashlib
import json
import shutil
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

from test_evaluation_record import read_psql_set
from test_evaluation_run import EVALUATION, ROOT, EvalHost, _invoke, _run_kwargs

from gideon.evaluation.judgments import Judgment, serialize
from gideon.evaluation.rankmetrics import DEFINITION_ID

SENTINEL = "[FICTIONAL TEST ONLY] this question must never enter a stream"


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


def _write_sql(host: EvalHost) -> str:
    writes = [
        input
        for argv, input in host.calls
        if argv[0] == "docker" and input != "SELECT 1;\n"
    ]
    if len(writes) != 1 or writes[0] is None:
        raise AssertionError(f"expected one SQL write, got {len(writes)}")
    return writes[0]


def _summary_line(stdout: str, prefix: str) -> str:
    """The one summary line starting with *prefix*, so a stray id is caught."""

    lines = [line for line in stdout.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, lines
    return lines[0]


class Command(unittest.TestCase):
    """The engine-free judgments runner follows the recorded command path."""

    def test_records_rows_figures_summary_and_overrides_without_case_text(self) -> None:
        grades = {1: 3, 2: 1, 3: 0, 4: 2, 5: None}
        with tempfile.TemporaryDirectory() as directory:
            checkout, ids = _fixture(directory, grades)
            ranked_path, ranked_bytes = _ranked_file(
                directory, {1: (1,), 2: (2,), 3: (3,), 4: (), 5: ()}
            )
            host = EvalHost()
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
            sql = _write_sql(host)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn("INSERT", stdout)
        self.assertIn("ranked: ok", stdout)
        self.assertIn("5 queries, 3 passages", stdout)
        self.assertIn("judged queries 4 of 5; definition judgments@1", stdout)
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
        self.assertIn("small set: 4 judged queries below 25", stdout)
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
        overrides_line = next(line for line in sql.splitlines() if line.startswith("\\set overrides "))
        self.assertEqual(
            json.loads(read_psql_set(overrides_line, "overrides")),
            {
                "judgments": {
                    "definition": DEFINITION_ID,
                    "ranked_sha256": hashlib.sha256(ranked_bytes).hexdigest(),
                }
            },
        )
        self.assertNotIn(SENTINEL, stdout + stderr + sql)

    def test_small_set_line_drops_at_the_ranking_floor(self) -> None:
        grades = dict.fromkeys(range(1, 26), 2)
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, grades)
            ranked_path, _ranked_bytes = _ranked_file(
                directory, {index: (index,) for index in grades}
            )
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)],
                **_run_kwargs(
                    EvalHost(), checkout=checkout, court_path=checkout / "courts.yaml"
                ),
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("judged queries 25 of 25", stdout)
        self.assertNotIn("small set:", stdout)

    def test_missing_ranked_query_records_fail_and_red_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 2})
            ranked_path = _write_ranked_bytes(directory, b"")
            host = EvalHost()
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--ranked", str(ranked_path)],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
            sql = _write_sql(host)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertIn("record: ok — run", stdout)
        self.assertIn("\\set result_0_verdict 'fail'", sql)
        self.assertIn("gate: refuse", stdout)
        # The uncovered query is reported by id, the verdict being coverage.
        self.assertEqual(
            _summary_line(stdout, "judged and not ranked:"),
            "judged and not ranked: judgments-001",
        )

    def test_double_graded_passage_uses_primary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3}, second_grades={1: 0})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            host = EvalHost()
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
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 2})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            host = EvalHost()
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
                ],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("record: ok — skipped", stdout)
        self.assertEqual([call for call in host.calls if call[0][0] == "docker"], [])


class NoText(unittest.TestCase):
    """The runner modules use ids and coordinates, never case prose fields."""

    def test_new_modules_do_not_subscript_or_get_content_fields(self) -> None:
        forbidden = {"question", "notes", "labels", "text", "caption", "quote"}
        for name in ("judgments_slice.py", "ranked.py", "rankmetrics.py"):
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
