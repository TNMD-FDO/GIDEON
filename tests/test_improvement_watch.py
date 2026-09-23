"""Trigger evaluation and eval-set figures for the read-only report (§19.4)."""

import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.evaluation import evalset, judgments
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike
from gideon.improvement import triggers
from gideon.improvement.feedback import FeedbackReading
from gideon.improvement.measures import READERS
from gideon.improvement.sections import Context
from gideon.improvement.watch import (
    evaluate_clause,
    evaluate_trigger,
    render_row,
)

ROOT = Path(__file__).resolve().parent.parent


def _clause(
    op: triggers.TriggerOp,
    *,
    value: int | float = 4,
    runs: int = 1,
    figure: str = "fictional_metric",
) -> triggers.Clause:
    return triggers.Clause(figure=figure, op=op, value=value, runs=runs)


def _trigger(*clauses: triggers.Clause, baseline: int | float | None = None) -> triggers.Trigger:
    return triggers.Trigger(
        id="fictional-trigger",
        reopens="§99.9",
        register="E99",
        measure="unavailable",
        state="watching",
        condition=triggers.Condition(tuple(clauses)),
        says="This sentence is fictitious and must not appear in a row.",
        ruled=None,
        baseline=baseline,
    )


class ClauseEvaluation(unittest.TestCase):
    """The condition grammar's comparisons and release counts are deterministic."""

    def test_operator_boundaries(self) -> None:
        cases: tuple[tuple[triggers.TriggerOp, int, str], ...] = (
            ("above", 3, "fails"),
            ("above", 4, "fails"),
            ("above", 5, "holds"),
            ("at-least", 3, "fails"),
            ("at-least", 4, "holds"),
            ("at-least", 5, "holds"),
            ("below", 3, "holds"),
            ("below", 4, "fails"),
            ("below", 5, "fails"),
        )
        for op, number, expected in cases:
            with self.subTest(op=op, number=number):
                verdict = evaluate_clause(_clause(op), {"fictional_metric": (number,)})
                self.assertEqual(verdict.state, expected)
                self.assertEqual(verdict.values, (number,))
                self.assertEqual(verdict.measured, 1)

    def test_runs_accept_enough_values_and_refuse_too_few(self) -> None:
        clause = _clause("above", value=2, runs=2)
        enough = evaluate_clause(clause, {"fictional_metric": (3, 4, 1)})
        self.assertEqual(enough.state, "holds")
        self.assertEqual(enough.values, (3, 4))
        self.assertEqual(enough.measured, 2)

        exactly = evaluate_clause(clause, {"fictional_metric": (3, 4)})
        self.assertEqual(exactly.state, "holds")
        self.assertEqual(exactly.values, (3, 4))

        too_few = evaluate_clause(clause, {"fictional_metric": (3,)})
        self.assertEqual(too_few.state, "unmeasurable")
        self.assertEqual(too_few.measured, 1)

    def test_missing_figure_is_unmeasurable(self) -> None:
        verdict = evaluate_clause(_clause("above"), {})
        self.assertEqual(verdict.state, "unmeasurable")
        self.assertEqual(verdict.values, ())
        self.assertEqual(verdict.measured, 0)


class TriggerEvaluation(unittest.TestCase):
    """A failed clause takes precedence over an unmeasurable clause."""

    def test_trigger_verdict_precedence(self) -> None:
        failing_and_missing = _trigger(
            _clause("above", figure="failure"),
            _clause("above", figure="missing"),
        )
        self.assertEqual(
            evaluate_trigger(failing_and_missing, {"failure": (0,)}).state,
            "not fired",
        )

        holding = _trigger(_clause("at-least", value=4))
        self.assertEqual(
            evaluate_trigger(holding, {"fictional_metric": (4,)}).state,
            "fired",
        )

        not_measurable = _trigger(_clause("at-least"))
        self.assertEqual(evaluate_trigger(not_measurable, {}).state, "not yet measurable")

    def test_rows_render_each_verdict_baseline_and_run_count(self) -> None:
        trigger = _trigger(_clause("above", value=2, runs=2), baseline=1)
        cases: tuple[tuple[Mapping[str, tuple[int | float, ...]], str, str], ...] = (
            ({"fictional_metric": (3, 4)}, "fired", "2 of 2 measured"),
            ({"fictional_metric": (1, 3)}, "not fired", "2 of 2 measured"),
            ({"fictional_metric": (3,)}, "not yet measurable", "1 of 2 measured"),
        )
        for figures, state, measured in cases:
            with self.subTest(state=state):
                row = render_row(evaluate_trigger(trigger, figures))
                self.assertEqual(row.name, trigger.id)
                self.assertEqual(row.state, state)
                self.assertIn("fictional_metric", row.detail)
                self.assertIn("on 2 runs", row.detail)
                self.assertIn(measured, row.detail)
                self.assertIn("reopens §99.9 (E99)", row.detail)
                self.assertIn("baseline 1", row.detail)
                self.assertNotIn(trigger.says, row.detail)
        missing = render_row(evaluate_trigger(trigger, {}))
        self.assertIn("unmeasured", missing.detail)


class MeasureHost:
    """Dict-backed filesystem seam for committed eval-set measure reads."""

    def __init__(
        self,
        files: Mapping[str, str] | None = None,
        directories: tuple[str, ...] = (),
    ) -> None:
        self.files = dict(files or {})
        self.directories = set(directories)

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, env, timeout, passthrough
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.files or key in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path).rstrip("/")
        if key not in self.directories:
            raise FileNotFoundError(key)
        prefix = f"{key}/"
        children = {
            child[len(prefix) :].split("/", 1)[0]
            for child in (*self.files, *self.directories)
            if child.startswith(prefix) and child != key
        }
        return sorted(children)


def _context(host: MeasureHost) -> Context:
    registry = triggers.TriggerRegistry(1, (), {}, ())
    return Context(
        host=host,  # type: ignore[arg-type]
        checkout_root=ROOT,
        rendered_dir=Path("/etc/gideon/rendered"),
        registry=registry,
        build_box=False,
        query=lambda _sql: (),
        feedback=lambda: FeedbackReading((), 0),
        now=lambda: 0.0,
    )


def _judgment(query_id: str, index: int, assessment: str) -> judgments.Judgment:
    return judgments.Judgment(
        query_id=query_id,
        source_id=f"fictional/source-{index}",
        sha256=f"{index:064x}",
        start=0,
        end=1,
        grade=2,
        grader="CHU-attorney-1" if assessment == "primary" else "TRAD-attorney-1",
        assessment=assessment,
    )


class EvalSetReader(unittest.TestCase):
    """The eval-set reader counts unique ids without exposing judgments text."""

    def setUp(self) -> None:
        self.set_root = ROOT / evalset.SET_ROOT
        self.judgments_path = self.set_root / judgments.JUDGMENTS_PATH
        self.held_out = self.set_root / "slices" / "judgments-held-out"

    def test_absent_judgments_file_is_zero_without_a_parse(self) -> None:
        host = MeasureHost()
        result = READERS["eval-set"].read(_context(host))
        self.assertEqual(result, {"judged_queries": (0,), "held_out_ids": (0,)})

    def test_primary_count_deduplicates_query_ids_and_excludes_second_grades(self) -> None:
        records = (
            _judgment("judgments-001", 1, "primary"),
            _judgment("judgments-001", 1, "second"),
            _judgment("judgments-002", 2, "primary"),
        )
        text = "".join(judgments.serialize(record) for record in records)
        host = MeasureHost(
            {os.fspath(self.judgments_path): text},
            (os.fspath(self.held_out),),
        )
        result = READERS["eval-set"].read(_context(host))
        self.assertEqual(result, {"judged_queries": (2,), "held_out_ids": (0,)})

    def test_malformed_judgments_return_the_parser_fix_without_line_text(self) -> None:
        sentinel = "FICTIONAL_MALFORMED_JUDGMENTS_LINE"
        host = MeasureHost(
            {os.fspath(self.judgments_path): f"{sentinel}\n"},
            (os.fspath(self.held_out),),
        )
        result = READERS["eval-set"].read(_context(host))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertEqual(result.fix, judgments.JUDGMENT_LINE_FIX)
        self.assertNotIn(sentinel, result.problem)
        self.assertNotIn(sentinel, result.fix)

    def test_held_out_directory_absent_empty_and_shared_ids(self) -> None:
        absent = READERS["eval-set"].read(_context(MeasureHost()))
        self.assertEqual(absent, {"judged_queries": (0,), "held_out_ids": (0,)})

        empty_host = MeasureHost(directories=(os.fspath(self.held_out),))
        empty = READERS["eval-set"].read(_context(empty_host))
        self.assertEqual(empty, {"judged_queries": (0,), "held_out_ids": (0,)})

        first = self.held_out / "first.ids"
        second = self.held_out / "second.ids"
        shared_host = MeasureHost(
            {
                os.fspath(first): "fictional-id\nshared-id\n",
                os.fspath(second): " shared-id \nother-id\n",
            },
            (os.fspath(self.held_out),),
        )
        shared = READERS["eval-set"].read(_context(shared_host))
        self.assertEqual(shared, {"judged_queries": (0,), "held_out_ids": (3,)})


class ReaderVocabulary(unittest.TestCase):
    """Every available measure declares exactly its vocabulary figures."""

    def test_readers_match_the_measure_registry(self) -> None:
        expected = set(triggers.MEASURES) - {"unavailable"}
        self.assertEqual(set(READERS), expected)
        for measure, reader in READERS.items():
            with self.subTest(measure=measure):
                self.assertEqual(reader.figures, triggers.MEASURES[measure])


if __name__ == "__main__":
    unittest.main()
