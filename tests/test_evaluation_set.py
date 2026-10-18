"""Eval-set loading contracts."""

import ast
import hashlib
import json
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from gideon.evaluation import judgments, segmenter_labels, signoffs, synthetic
from gideon.evaluation.evalset import (
    GOLD_COORDINATE_KEYS,
    GUARDRAIL_ID_PATTERN,
    SEGMENTER_TEXT_SOURCES,
    SET_ROOT,
    SHAPE_REGISTRY,
    TIER_2_CATEGORY,
    Finding,
    LoadedSet,
    ShapeSpec,
    case_findings,
    held_out_path,
    load_set,
    select_cases,
    tuning_cases,
)
from gideon.evaluation.judgments import Judgment, serialize
from gideon.evaluation.signoffs import SignOff
from gideon.extraction.scoring import active_cases
from gideon.host.courts import load_court_map
from gideon.host.index.treatment import PATTERN_SET_PATTERN
from gideon.host.render.worker import SECTION_TYPES, TREATMENT_STATES
from gideon.worker.opiniontext import TEXT_SOURCES
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "[FICTIONAL TEST QUESTION — never include this text in a finding]"
# Every module that tunes a release artifact against the set, by repository
# path: each must import tuning_cases and never call select_cases.
TUNING_TOOLS: tuple[str, ...] = ()


def _write(root: Path, relative: str, text: str, *, final_newline: bool = True) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + ("\n" if final_newline else ""), encoding="utf-8")


def _case(case_id: str, question: str = SENTINEL) -> str:
    return json.dumps(
        {
            "id": case_id,
            "suite": "build-gates",
            "category": "extraction",
            "branch": "legal",
            "question": question,
            "expected": {"objects": []},
            "labels": ["invented"],
            "cluster_id": case_id,
            "review": {"by": "CSA-1", "on": "2026-09-19"},
            "notes": "",
        },
        separators=(",", ":"),
    )


def _harvest_extraction_case(case_id: str = "extraction-001") -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "build-gates",
        "category": "extraction",
        "branch": "legal",
        "question": SENTINEL,
        "expected": {"objects": []},
        "labels": ["harvest", "doctrinal"],
        "seed": "HARV-001",
        "cluster_id": "harvest-chat-abcdef",
        "review": {"by": "CSA-1", "on": "2026-09-19"},
        "notes": "",
    }


def _variant_case(
    case_id: str = "extraction-002",
    parent: str = "extraction-001",
    axis: str = "sign-present@1",
    cluster_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "build-gates",
        "category": "extraction",
        "branch": "legal",
        "question": SENTINEL,
        "expected": {"objects": []},
        "labels": ["variant", axis],
        "parent": parent,
        "cluster_id": parent if cluster_id is None else cluster_id,
        "review": {"by": "CSA-1", "on": "2026-09-19"},
        "notes": "",
    }


def _write_cases(root: Path, records: tuple[dict[str, object], ...]) -> None:
    lines = [json.dumps(record, separators=(",", ":")) for record in records]
    _write(root, "build-gates/cases.jsonl", "\n".join(lines))


def count_gate_case(case_id: str = "count-gates-staged-01") -> dict[str, object]:
    """One fictitious rule case with the public shape's ordered fields."""

    return {
        "id": case_id,
        "suite": "build-gates",
        "category": "count-gates",
        "branch": "legal",
        "rule": "Every fictional court has its fictional count recorded.",
        "labels": ["spec"],
        "cluster_id": case_id,
        "review": {"by": "CSA-1", "on": "2099-01-03"},
    }


def _write_research_cases(root: Path, records: tuple[dict[str, object], ...]) -> None:
    lines = [json.dumps(record, separators=(",", ":")) for record in records]
    _write(root, "research-qa/cases.jsonl", "\n".join(lines))


def _scorer_active_ids(loaded: LoadedSet) -> tuple[str, ...]:
    """The scorer's active set as sorted ids, the loader's held equal to it."""

    files = tuple(loaded.cases_by_file.values())
    return tuple(sorted(str(case["id"]) for case in active_cases(files)))


def _judgment(query_id: str = "judgments-001", grade: int = 2) -> Judgment:
    return Judgment(
        query_id=query_id,
        source_id="fictional/source-1",
        sha256="a" * 64,
        start=0,
        end=4,
        grade=grade,
        grader="CHU-attorney-1",
        assessment="primary",
    )


def _write_judgment(root: Path, record: Judgment) -> None:
    _write(root, judgments.JUDGMENTS_PATH.as_posix(), serialize(record).rstrip("\n"))


def _research_qa_case(
    case_id: str = "research-qa-001",
    category: str = "retrieval",
    question: str = SENTINEL,
) -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "research-qa",
        "category": category,
        "branch": "legal",
        "question": question,
        "labels": ["harvest", "rewritten", "other"],
        "seed": "HARV-001",
        "cluster_id": "harvest-chat-abcdef",
        "notes": "",
        "review": {"by": "CSA-1", "on": "2026-09-21", "accepted_flags": []},
    }


def _signoff(
    case_id: str = "research-qa-001", answer: str = "The fictional signed answer."
) -> SignOff:
    return SignOff(case_id, answer, ("fictional/source-1",), "CHU-attorney-1", "2026-09-21")


def _synthetic_case(case_id: str | None = None, *, category: str = "lookup") -> dict[str, object]:
    quote = "The fictitious court required notice.\n\nIt remanded the invented case. 😀"
    expected: dict[str, object] = {"answer": SENTINEL, "must_cite": ["fictional/source-1"]}
    gold: dict[str, object] = {
        "source_id": "fictional/source-1", "sha256": "a" * 64,
        "start": 5, "end": 5 + len(quote), "quote": quote,
    }
    if category == "treatment":
        expected["state"] = TREATMENT_STATES[0]
        gold.update({
            "citation_id": "c" * 64, "signal_id": "d" * 64,
            "pattern_set": "treatment/patterns@999", "cluster": 123,
        })
    elif category == "section-attribution":
        expected["section_type"] = SECTION_TYPES[0]
    elif category in synthetic.NEAR_MISS_CATEGORIES:
        expected = {"answer": SENTINEL, "behavior": synthetic.BEHAVIORS[category]}
        if category == "poisoned-premise":
            expected["premise"] = "Fictitious v. Imaginary"
    review: dict[str, object] = {"by": "CSA-1", "on": "2001-01-02"}
    if not SHAPE_REGISTRY[("research-qa", category)].zero_tolerance:
        review["read"] = True
    return {
        "id": f"research-qa-{category}-001" if case_id is None else case_id,
        "suite": "research-qa",
        "category": category,
        "branch": "legal",
        "question": SENTINEL,
        "jurisdiction": ["ca6"],
        "expected": expected,
        "gold": [gold],
        "labels": [synthetic.SYNTHETIC_ORIGIN],
        "template": f"{category}@1",
        "cluster_id": "fictional/source-1",
        "review": review,
        "notes": "Fictitious case for the loader.",
    }


def _write_signoffs(root: Path, records: tuple[SignOff, ...]) -> None:
    _write(
        root,
        signoffs.SIGNOFFS_PATH.as_posix(),
        "".join(signoffs.serialize(record) for record in records).rstrip("\n"),
    )


def _segmenter_case(case_id: str = "segmenter-001") -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "build-gates",
        "category": "segmenter-300",
        "branch": "legal",
        "opinion_id": 1,
        "court": "ca6",
        "decade": 2000,
        "text_source": SEGMENTER_TEXT_SOURCES[0],
        "labels": ["drawn"],
        "cluster_id": case_id,
        "review": {"by": "CSA-1", "on": "2026-09-21"},
        "notes": "",
    }


def _segmenter_label(case_id: str = "segmenter-001") -> segmenter_labels.SegmenterLabel:
    return segmenter_labels.SegmenterLabel(
        case_id, "a" * 64, 4,
        (segmenter_labels.LabelSpan(0, 4, "majority"),),
        "CSA-1", "2026-09-21",
    )


def _write_segmenter_labels(
    root: Path, records: tuple[segmenter_labels.SegmenterLabel, ...]
) -> None:
    _write(
        root,
        segmenter_labels.LABELS_PATH.as_posix(),
        "".join(segmenter_labels.serialize(record) for record in records).rstrip("\n"),
    )


def _digest_model(root: Path) -> str:
    """Recompute the loader's sorted relative-path and bytes-digest contract."""

    suites = tuple(
        path for path in root.iterdir() if path.is_dir() and path.name != "slices"
    )
    paths = [path for suite in suites for path in suite.rglob("*.jsonl") if path.is_file()]
    paths.extend(path for suite in suites for path in suite.rglob("*.held-out.ids") if path.is_file())
    slices = root / "slices"
    if slices.is_dir():
        paths.extend(path for path in slices.rglob("*.ids") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        bytes_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


class Refusals(unittest.TestCase):
    """Loader refusals name only content-free locations and fixes."""

    def assert_findings(self, root: Path, *expected: str) -> tuple[Finding, ...]:
        result = load_set(root)
        self.assertIsNone(result.loaded)
        rendered = "\n".join(finding.text() for finding in result.findings)
        for text in expected:
            self.assertIn(text, rendered)
        self.assertNotIn(SENTINEL, rendered)
        return result.findings

    def test_duplicate_id_names_both_case_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "build-gates/a.jsonl", _case("extraction-001"))
            _write(root, "build-gates/b.jsonl", _case("extraction-001"))
            _write(root, "slices/extraction/cases.ids", "extraction-001")
            findings = self.assert_findings(root, "a.jsonl", "b.jsonl", "extraction-001")
            self.assertEqual(len(findings), 1)

    def test_slice_id_resolving_to_no_case_names_the_id_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "build-gates/cases.jsonl", _case("extraction-001"))
            _write(root, "slices/extraction/cases.ids", "missing-case")
            findings = self.assert_findings(root, "cases.ids", "missing-case")
            self.assertEqual(len(findings), 1)

    def test_id_list_cannot_repeat_an_id_in_one_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "build-gates/cases.jsonl", _case("extraction-001"))
            _write(root, "slices/extraction/cases.ids", "extraction-001\nextraction-001")
            findings = self.assert_findings(root, "cases.ids", "listed twice")
            self.assertEqual(len(findings), 1)

    def test_slice_directory_requires_an_id_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "build-gates/cases.jsonl", _case("extraction-001"))
            (root / "slices" / "extraction").mkdir(parents=True)
            findings = self.assert_findings(root, "slices/extraction", "no id list")
            self.assertEqual(len(findings), 1)

    def test_held_out_findings_are_collected_together_without_question_text(self) -> None:
        cases_file = Path("build-gates/cases.jsonl")
        cases_list = held_out_path(cases_file).as_posix()
        variant_file = Path("build-gates/a-variants.jsonl")
        variant_list = held_out_path(variant_file).as_posix()
        research_list = held_out_path(Path("research-qa/cases.jsonl")).as_posix()
        empty_list = held_out_path(Path("build-gates/empty.jsonl")).as_posix()
        stray_list = held_out_path(judgments.JUDGMENTS_PATH).as_posix()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (
                _harvest_extraction_case("extraction-001"),
                json.loads(_case("extraction-003")),
                json.loads(_superseding("extraction-004", "extraction-003")),
            ))
            _write(root, variant_file.as_posix(), json.dumps(_variant_case("extraction-005", "extraction-004")))
            _write_research_cases(root, (_research_qa_case(),))
            _write(root, "build-gates/empty.jsonl", _case("extraction-006"))
            _write(root, "judgments/queries.jsonl", json.dumps(_judgment_case()))
            _write_judgment(root, _judgment())
            _write(root, cases_list, "extraction-001\nextraction-003\nresearch-qa-001\nextraction-001\n")
            _write(root, empty_list, "")
            _write(root, stray_list, "judgments-001")
            findings = self.assert_findings(root, "held-out")

        expected = {
            (cases_list, "research-qa-001", 3): "held-out id resolves to no case of this file",
            (cases_list, "extraction-001", 4): f"id is listed twice in held-out list (also {cases_list}:1)",
            (cases_list, None, 5): "blank line in held-out list",
            (empty_list, None, 1): "blank line in held-out list",
            (research_list, "research-qa-001", None): "shares a family with held-out case extraction-001 and is not held out",
            (cases_list, "extraction-004", None): "shares a family with held-out case extraction-003 and is not held out",
            (variant_list, "extraction-005", None): "shares a family with held-out case extraction-003 and is not held out",
            (stray_list, None, 0): "held-out list beside no case file",
        }
        self.assertEqual(len(findings), len(expected))
        self.assertEqual({(item.file, item.id, item.line) for item in findings}, set(expected))
        for item in findings:
            with self.subTest(file=item.file, case_id=item.id, line=item.line):
                self.assertIn(expected[(item.file, item.id, item.line)], item.rule)
                self.assertEqual(item.fix, "Correct the named held-out list, then retry.")
                self.assertIn(item.file, item.text())
                self.assertIn(item.rule, item.text())
                self.assertIn(item.id if item.id is not None else f"line {item.line}", item.text())
        family_findings = [item for item in findings if "shares a family" in item.rule]
        self.assertTrue(all("cluster_id" in item.rule for item in family_findings))

    def test_held_out_list_requires_a_final_newline(self) -> None:
        relative = held_out_path(Path("build-gates/cases.jsonl")).as_posix()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (json.loads(_case("extraction-001")),))
            _write(root, relative, "extraction-001", final_newline=False)
            findings = self.assert_findings(root, relative, "file has no final newline")
        self.assertEqual(len(findings), 1)
        self.assertEqual((findings[0].file, findings[0].id, findings[0].line), (relative, None, 0))
        self.assertEqual(findings[0].fix, "Correct the named held-out list, then retry.")

    def test_non_json_and_missing_final_newline_findings_are_collected_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "suite/a.jsonl", "not one JSON object")
            _write(root, "build-gates/b.jsonl", _case("extraction-002"), final_newline=False)
            findings = self.assert_findings(root, "a.jsonl", "line 1", "b.jsonl", "final newline")
            locations = [(finding.file, finding.line or 0) for finding in findings]
            self.assertEqual(locations, sorted(locations))
            self.assertEqual(len(findings), 2)

    def test_judgments_file_is_not_loaded_as_a_case_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(
                root,
                "judgments/queries.jsonl",
                json.dumps(_judgment_case(), separators=(",", ":")),
            )
            _write_judgment(root, _judgment())
            result = load_set(root)

            self.assertTrue(result.ok, result.findings)
            assert result.loaded is not None
            self.assertEqual(tuple(result.loaded.cases_by_file), ("judgments/queries.jsonl",))
            self.assertEqual(result.loaded.judgments, (_judgment(),))

    def test_unknown_judgment_query_is_a_finding_by_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(
                root,
                "judgments/queries.jsonl",
                json.dumps(_judgment_case(), separators=(",", ":")),
            )
            _write_judgment(root, _judgment("judgments-999"))
            result = load_set(root)

            self.assertIsNone(result.loaded)
            findings = [
                finding
                for finding in result.findings
                if finding.rule == "query_id names no case"
            ]
            self.assertEqual(
                [(finding.file, finding.line) for finding in findings],
                [(judgments.JUDGMENTS_PATH.as_posix(), 1)],
            )

    def test_superseded_judgment_query_is_a_finding_by_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            first = _judgment_case("judgments-001")
            successor = _judgment_case("judgments-002")
            successor["supersedes"] = "judgments-001"
            _write(
                root,
                "judgments/queries.jsonl",
                "\n".join(
                    json.dumps(record, separators=(",", ":"))
                    for record in (first, successor)
                ),
            )
            _write_judgment(root, _judgment("judgments-001"))
            result = load_set(root)

            self.assertIsNone(result.loaded)
            findings = [
                finding
                for finding in result.findings
                if finding.rule == "query_id names a superseded case"
            ]
            self.assertEqual(
                [(finding.file, finding.line) for finding in findings],
                [(judgments.JUDGMENTS_PATH.as_posix(), 1)],
            )

    def test_other_suite_judgment_query_is_a_finding_by_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "build-gates/cases.jsonl", _case("judgments-003"))
            _write_judgment(root, _judgment("judgments-003"))
            result = load_set(root)

            self.assertIsNone(result.loaded)
            findings = [
                finding
                for finding in result.findings
                if finding.rule == "query_id names a case of another suite"
            ]
            self.assertEqual(
                [(finding.file, finding.line) for finding in findings],
                [(judgments.JUDGMENTS_PATH.as_posix(), 1)],
            )

    def test_malformed_judgment_grade_is_one_judgments_finding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(
                root,
                "judgments/queries.jsonl",
                json.dumps(_judgment_case(), separators=(",", ":")),
            )
            record = json.loads(serialize(_judgment()))
            record["grade"] = "fictional-grade"
            _write(
                root,
                judgments.JUDGMENTS_PATH.as_posix(),
                json.dumps(record, separators=(",", ":")),
            )
            result = load_set(root)

            self.assertIsNone(result.loaded)
            findings = tuple(result.findings)
            self.assertEqual(len(findings), 1)
            self.assertEqual(
                (findings[0].file, findings[0].line, findings[0].rule),
                (judgments.JUDGMENTS_PATH.as_posix(), 1, "grade"),
            )

    def test_missing_final_newline_is_the_judgments_finding_alone(self) -> None:
        """One fault is one finding: the loader defers to the module's words."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(
                root,
                "judgments/queries.jsonl",
                json.dumps(_judgment_case(), separators=(",", ":")),
            )
            _write(
                root,
                judgments.JUDGMENTS_PATH.as_posix(),
                serialize(_judgment()).rstrip("\n"),
                final_newline=False,
            )
            result = load_set(root)
            expected = judgments.parse_bytes(
                (root / judgments.JUDGMENTS_PATH).read_bytes(),
                judgments.JUDGMENTS_PATH.as_posix(),
            ).findings

            self.assertIsNone(result.loaded)
            self.assertEqual(
                tuple(finding.rule for finding in expected),
                ("file has no final newline",),
            )
            self.assertEqual(tuple(result.findings), expected)


class HeldOutCases(unittest.TestCase):
    """Held-out lists preserve case families while gates count both partitions."""

    def test_sidecar_ids_are_loaded_and_one_changed_byte_moves_the_digest(self) -> None:
        relative = "build-gates/cases.held-out.ids"
        self.assertEqual(held_out_path(Path("build-gates/cases.jsonl")), Path(relative))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (
                json.loads(_case("extraction-001")),
                json.loads(_case("extraction-002")),
            ))
            _write(root, relative, "extraction-001")
            before = load_set(root)
            self.assertTrue(before.ok, before.findings)
            assert before.loaded is not None
            self.assertEqual(before.loaded.held_out_ids, frozenset({"extraction-001"}))
            self.assertEqual(tuple(before.loaded.cases_by_file), ("build-gates/cases.jsonl",))
            old_bytes = (root / relative).read_bytes()
            _write(root, relative, "extraction-002")
            new_bytes = (root / relative).read_bytes()
            self.assertEqual(sum(left != right for left, right in zip(old_bytes, new_bytes, strict=True)), 1)
            after = load_set(root)
            self.assertTrue(after.ok, after.findings)
            assert after.loaded is not None
            self.assertEqual(after.loaded.held_out_ids, frozenset({"extraction-002"}))
            self.assertEqual(before.loaded.cases_by_file, after.loaded.cases_by_file)
            self.assertEqual(before.loaded.active_ids, after.loaded.active_ids)
            self.assertNotEqual(before.loaded.digest, after.loaded.digest)

    def test_whole_family_removal_includes_the_retired_seed_and_partial_removal_refuses(self) -> None:
        cases_list = held_out_path(Path("build-gates/cases.jsonl"))
        variants_list = held_out_path(Path("build-gates/a-variants.jsonl"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (
                json.loads(_case("extraction-001")),
                json.loads(_superseding("extraction-003", "extraction-001")),
                json.loads(_case("extraction-005")),
            ))
            _write(root, "build-gates/a-variants.jsonl", json.dumps(_variant_case("extraction-004", "extraction-003")))
            _write(root, "slices/demo/cases.ids", "extraction-001\nextraction-003\nextraction-004\nextraction-005")
            _write(root, cases_list.as_posix(), "extraction-001\nextraction-003")
            _write(root, variants_list.as_posix(), "extraction-004")
            held = load_set(root)
            self.assertTrue(held.ok, held.findings)
            assert held.loaded is not None
            self.assertEqual(held.loaded.held_out_ids, frozenset({"extraction-001", "extraction-003", "extraction-004"}))
            self.assertNotIn("extraction-001", held.loaded.active_ids)
            self.assertEqual(select_cases(held.loaded, "demo").held_out, ("extraction-003", "extraction-004"))

            # Keeping only the retired seed still binds its successor and variant.
            _write(root, cases_list.as_posix(), "extraction-001")
            (root / variants_list).unlink()
            partial = load_set(root)
            self.assertIsNone(partial.loaded)
            self.assertEqual(
                {(item.file, item.id, item.line) for item in partial.findings},
                {(cases_list.as_posix(), "extraction-003", None), (variants_list.as_posix(), "extraction-004", None)},
            )
            self.assertEqual(len(partial.findings), 2)
            for item in partial.findings:
                self.assertIn("shares a family with held-out case extraction-001 and is not held out", item.rule)
                self.assertEqual(item.fix, "Correct the named held-out list, then retry.")
                self.assertNotIn(SENTINEL, item.text())

            (root / cases_list).unlink()
            removed = load_set(root)
            self.assertTrue(removed.ok, removed.findings)
            assert removed.loaded is not None
            self.assertEqual(removed.loaded.held_out_ids, frozenset())
            selection = select_cases(removed.loaded, "demo")
            self.assertEqual(selection.counted, select_cases(held.loaded, "demo").counted)
            self.assertEqual(selection.tuning, ("extraction-003", "extraction-004", "extraction-005"))
            self.assertEqual(selection.held_out, ())
            self.assertEqual(tuning_cases(removed.loaded, "demo"), selection.counted)

    def test_selection_partitions_counted_in_slice_order_without_changing_exclusions(self) -> None:
        ids = ("extraction-005", "extraction-002", "extraction-004", "extraction-001", "extraction-003")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, tuple(json.loads(_case(case_id)) for case_id in ids))
            _write(root, "slices/demo/cases.ids", "\n".join(ids))
            before = load_set(root)
            self.assertTrue(before.ok, before.findings)
            assert before.loaded is not None
            baseline = select_cases(replace(before.loaded, slices={"demo": ids}), "demo")
            relative = held_out_path(Path("build-gates/cases.jsonl")).as_posix()
            _write(root, relative, "extraction-002\nextraction-004")
            after = load_set(root)
            self.assertTrue(after.ok, after.findings)
            assert after.loaded is not None
            selection = select_cases(replace(after.loaded, slices={"demo": ids}), "demo")
        self.assertEqual(selection.counted, ids)
        self.assertEqual(selection.counted, baseline.counted)
        self.assertEqual(selection.unsigned, baseline.unsigned)
        self.assertEqual(selection.takes_signoff, baseline.takes_signoff)
        self.assertEqual(selection.tuning, ("extraction-005", "extraction-001", "extraction-003"))
        self.assertEqual(selection.held_out, ("extraction-002", "extraction-004"))
        self.assertFalse(set(selection.tuning) & set(selection.held_out))
        self.assertEqual(set(selection.tuning) | set(selection.held_out), set(selection.counted))

    def test_tuning_cases_excludes_the_held_out_family(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (
                json.loads(_case("extraction-001")),
                _variant_case(),
                json.loads(_case("extraction-003")),
            ))
            _write(root, "slices/demo/cases.ids", "extraction-001\nextraction-002\nextraction-003")
            relative = held_out_path(Path("build-gates/cases.jsonl")).as_posix()
            _write(root, relative, "extraction-001\nextraction-002")
            result = load_set(root)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        self.assertEqual(tuning_cases(result.loaded, "demo"), ("extraction-003",))
        self.assertEqual(select_cases(result.loaded, "demo").counted, ("extraction-001", "extraction-002", "extraction-003"))
        self.assertTrue(set(tuning_cases(result.loaded, "demo")).isdisjoint(result.loaded.held_out_ids))

    def test_committed_slices_have_all_counted_cases_available_for_tuning(self) -> None:
        result = load_set(ROOT / SET_ROOT)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        for slice_name in result.loaded.slices:
            with self.subTest(slice=slice_name):
                self.assertEqual(tuning_cases(result.loaded, slice_name), select_cases(result.loaded, slice_name).counted)

    def test_tuning_tools_import_tuning_cases_and_never_call_select_cases(self) -> None:
        for relative in TUNING_TOOLS:
            with self.subTest(module=relative):
                path = ROOT / relative
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                imports = [
                    alias
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module == "gideon.evaluation.evalset"
                    for alias in node.names
                ]
                self.assertIn("tuning_cases", {alias.name for alias in imports})
                forbidden = {"select_cases"} | {
                    alias.asname or alias.name for alias in imports if alias.name == "select_cases"
                }
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    if isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, forbidden, f"{relative}:{node.lineno}")
                    elif isinstance(node.func, ast.Attribute):
                        self.assertNotEqual(node.func.attr, "select_cases", f"{relative}:{node.lineno}")


class OrderAndProvenance(unittest.TestCase):
    """Filesystem creation order never changes the loaded set or its digest."""

    def _make_clean_set(self, root: Path, reverse: bool = False) -> None:
        files = (
            ("build-gates/z.jsonl", _case("extraction-002")),
            ("build-gates/a.jsonl", _case("extraction-001")),
            ("slices/z-slice/cases.ids", "extraction-002"),
            ("slices/a-slice/cases.ids", "extraction-001"),
        )
        for relative, text in reversed(files) if reverse else files:
            _write(root, relative, text)

    def test_sorted_file_and_slice_order_is_provenance_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first" / "eval-v9"
            second = Path(directory) / "second" / "eval-v9"
            self._make_clean_set(first)
            self._make_clean_set(second, reverse=True)
            left = load_set(first).loaded
            right = load_set(second).loaded
            self.assertIsNotNone(left)
            self.assertIsNotNone(right)
            assert left is not None and right is not None
            self.assertEqual(tuple(left.cases_by_file), ("build-gates/a.jsonl", "build-gates/z.jsonl"))
            self.assertEqual(tuple(left.slices), ("a-slice", "z-slice"))
            self.assertEqual(left.cases_by_file, right.cases_by_file)
            self.assertEqual(left.cases_by_id, right.cases_by_id)
            self.assertEqual(left.slices, right.slices)
            self.assertEqual(left.slice_lists, right.slice_lists)
            self.assertEqual(
                left.slice_lists,
                {
                    "a-slice": {"cases": ("extraction-001",)},
                    "z-slice": {"cases": ("extraction-002",)},
                },
            )
            self.assertEqual(left.active_ids, right.active_ids)
            self.assertEqual(left.digest, right.digest)

    def test_findings_order_is_file_then_line_independent_of_creation_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first" / "eval-v9"
            second = Path(directory) / "second" / "eval-v9"
            _write(first, "build-gates/b.jsonl", "not JSON")
            _write(first, "build-gates/a.jsonl", "also not JSON")
            _write(second, "build-gates/a.jsonl", "also not JSON")
            _write(second, "build-gates/b.jsonl", "not JSON")
            first_result = load_set(first)
            second_result = load_set(second)
            first_findings = tuple(
                (finding.file, finding.line, finding.rule) for finding in first_result.findings
            )
            second_findings = tuple(
                (finding.file, finding.line, finding.rule) for finding in second_result.findings
            )
            self.assertEqual(first_findings, second_findings)

    def test_a_changed_case_byte_changes_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            self._make_clean_set(root)
            before = load_set(root).loaded
            self.assertIsNotNone(before)
            _write(root, "build-gates/a.jsonl", _case("extraction-001", "[FICTIONAL TEST QUESTION — changed]"))
            after = load_set(root).loaded
            self.assertIsNotNone(after)
            assert before is not None and after is not None
            self.assertNotEqual(before.digest, after.digest)

    def test_a_changed_id_list_byte_changes_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            self._make_clean_set(root)
            before = load_set(root).loaded
            self.assertIsNotNone(before)
            _write(root, "slices/a-slice/cases.ids", "extraction-001\nextraction-002")
            after = load_set(root).loaded
            self.assertIsNotNone(after)
            assert before is not None and after is not None
            self.assertNotEqual(before.digest, after.digest)
            self.assertNotEqual(before.slices, after.slices)

    @staticmethod
    def _non_signoff_shape() -> ShapeSpec:
        base = SHAPE_REGISTRY[("research-qa", "retrieval")]
        return ShapeSpec(
            "research-qa",
            "unsigned-test",
            base.base_keys,
            {"suite": "research-qa", "category": "unsigned-test", "branch": "legal"},
            base.id_pattern,
            base.cluster_pattern,
            base.review_keys,
            base.question_origins,
            False,
        )

    def test_signoff_relational_findings_are_by_line(self) -> None:
        first = _research_qa_case("research-qa-001")
        successor = _research_qa_case("research-qa-002")
        successor["supersedes"] = "research-qa-001"
        other = _research_qa_case("research-qa-003", "unsigned-test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_research_cases(root, (first, successor, other))
            _write_signoffs(
                root,
                (
                    _signoff("research-qa-999", answer=SENTINEL),
                    _signoff("research-qa-001", answer=SENTINEL),
                    _signoff("research-qa-003", answer=SENTINEL),
                ),
            )
            with patch.dict(
                SHAPE_REGISTRY,
                {("research-qa", "unsigned-test"): self._non_signoff_shape()},
            ):
                result = load_set(root)
        self.assertIsNone(result.loaded)
        findings = tuple(
            finding
            for finding in result.findings
            if finding.file == signoffs.SIGNOFFS_PATH.as_posix()
        )
        self.assertEqual(
            tuple((finding.line, finding.rule) for finding in findings),
            (
                (1, "case_id names no case"),
                (2, "case_id names a superseded case"),
                (3, "case_id names a case whose shape takes no sign-off"),
            ),
        )
        self.assertTrue(all(finding.fix == signoffs.SIGNOFF_LINE_FIX for finding in findings))
        self.assertTrue(all(finding.id is None for finding in findings))
        self.assertNotIn(SENTINEL, "\n".join(finding.text() for finding in findings))

    def test_unsigned_ids_and_selection_cover_signed_retired_and_other_shapes(self) -> None:
        records = [
            _research_qa_case("research-qa-001"),
            _research_qa_case("research-qa-002"),
            _research_qa_case("research-qa-003"),
            _research_qa_case("research-qa-004"),
            _research_qa_case("research-qa-005", "unsigned-test"),
        ]
        records[3]["supersedes"] = "research-qa-003"
        records[4]["cluster_id"] = "harvest-chat-123456"
        slice_ids = (
            "research-qa-001",
            "research-qa-002",
            "research-qa-003",
            "research-qa-004",
            "research-qa-005",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_research_cases(root, tuple(records))
            _write_signoffs(root, (_signoff("research-qa-001"),))
            _write(root, "slices/demo/cases.ids", "\n".join(slice_ids))
            relative = held_out_path(Path("research-qa/cases.jsonl")).as_posix()
            _write(root, relative, "\n".join(slice_ids[:4]))
            with patch.dict(
                SHAPE_REGISTRY,
                {("research-qa", "unsigned-test"): self._non_signoff_shape()},
            ):
                result = load_set(root)
                self.assertTrue(result.ok, result.findings)
                assert result.loaded is not None
                loaded = result.loaded
                selection = select_cases(loaded, "demo")
                tuning = tuning_cases(loaded, "demo")
        self.assertEqual(
            loaded.unsigned_ids, frozenset({"research-qa-002", "research-qa-004"})
        )
        self.assertEqual(
            selection.counted,
            ("research-qa-001", "research-qa-005"),
        )
        self.assertEqual(selection.unsigned, ("research-qa-002", "research-qa-004"))
        self.assertEqual(loaded.held_out_ids, frozenset(slice_ids[:4]))
        self.assertEqual(selection.held_out, ("research-qa-001",))
        self.assertEqual(selection.tuning, ("research-qa-005",))
        self.assertEqual(tuning, selection.tuning)
        self.assertTrue(selection.takes_signoff)
        self.assertEqual(loaded.signoffs, (_signoff("research-qa-001"),))

    def test_signoff_bytes_move_the_set_digest(self) -> None:
        case = _research_qa_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_research_cases(root, (case,))
            _write_signoffs(root, (_signoff(answer="First fictional answer."),))
            before = load_set(root).loaded
            _write_signoffs(root, (_signoff(answer="Second fictional answer."),))
            after = load_set(root).loaded
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        assert before is not None and after is not None
        self.assertNotEqual(before.digest, after.digest)

    def test_judgment_grade_byte_changes_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(
                root,
                "judgments/queries.jsonl",
                json.dumps(_judgment_case(), separators=(",", ":")),
            )
            _write_judgment(root, _judgment(grade=2))
            before = load_set(root).loaded
            self.assertIsNotNone(before)
            _write_judgment(root, _judgment(grade=3))
            after = load_set(root).loaded
            self.assertIsNotNone(after)
            assert before is not None and after is not None
            self.assertNotEqual(before.digest, after.digest)

    def test_loader_digest_matches_model_without_and_with_judgments_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            without = Path(directory) / "without" / "eval-v9"
            _write(
                without,
                "judgments/queries.jsonl",
                json.dumps(_judgment_case(), separators=(",", ":")),
            )
            loaded_without = load_set(without).loaded
            self.assertIsNotNone(loaded_without)
            assert loaded_without is not None
            self.assertEqual(loaded_without.digest, _digest_model(without))

            with_file = Path(directory) / "with" / "eval-v9"
            _write(
                with_file,
                "judgments/queries.jsonl",
                json.dumps(_judgment_case(), separators=(",", ":")),
            )
            _write_judgment(with_file, _judgment())
            loaded_with = load_set(with_file).loaded
            self.assertIsNotNone(loaded_with)
            assert loaded_with is not None
            self.assertEqual(loaded_with.digest, _digest_model(with_file))
            relative = held_out_path(Path("judgments/queries.jsonl")).as_posix()
            _write(with_file, relative, "judgments-001")
            loaded_with_sidecar = load_set(with_file).loaded
            self.assertIsNotNone(loaded_with_sidecar)
            assert loaded_with_sidecar is not None
            self.assertEqual(loaded_with_sidecar.digest, _digest_model(with_file))
            self.assertNotEqual(loaded_with_sidecar.digest, loaded_with.digest)

    def test_committed_set_loads_clean_in_whatever_tree_holds_it(self) -> None:
        court_map = load_court_map(ROOT / "courts.yaml").court_map
        assert court_map is not None
        result = load_set(ROOT / SET_ROOT, court_map.courts)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        harvest_absent = absent_from_export(SET_ROOT / "build-gates" / "extraction.jsonl", ROOT)
        self.assertEqual("build-gates/extraction.jsonl" in result.loaded.cases_by_file, not harvest_absent)
        extraction_ids = tuple(
            sorted(
                case["id"]
                for file, cases in result.loaded.cases_by_file.items()
                if file.startswith("build-gates/extraction")
                for case in cases
                if isinstance(case["id"], str)
            )
        )
        self.assertEqual(result.loaded.slices["extraction"], extraction_ids)
        judge_ids = tuple(
            sorted(
                case["id"]
                for file, cases in result.loaded.cases_by_file.items()
                if file.startswith("judge/")
                for case in cases
                if isinstance(case["id"], str)
            )
        )
        self.assertEqual(result.loaded.slices["judge-triples"], judge_ids)
        # The judgments slice leaves with the queries it names, so the set holds
        # it exactly where the boundary keeps that file.
        queries_absent = absent_from_export(SET_ROOT / "judgments" / "queries.jsonl", ROOT)
        self.assertEqual("judgments" in result.loaded.slices, not queries_absent)
        if not queries_absent:
            query_ids = tuple(
                sorted(
                    case["id"]
                    for file, cases in result.loaded.cases_by_file.items()
                    if file.startswith("judgments/")
                    for case in cases
                    if isinstance(case["id"], str)
                )
            )
            self.assertEqual(result.loaded.slices["judgments"], query_ids)
        research_absent = absent_from_export(
            SET_ROOT / "research-qa" / "harvest.jsonl", ROOT
        )
        self.assertEqual(
            "research-qa/harvest.jsonl" in result.loaded.cases_by_file,
            not research_absent,
        )
        if not research_absent:
            research_ids = tuple(
                sorted(
                    case["id"]
                    for file, cases in result.loaded.cases_by_file.items()
                    if file == "research-qa/harvest.jsonl"
                    for case in cases
                    if isinstance(case["id"], str)
                )
            )
            self.assertEqual(research_ids, tuple(f"research-qa-{i:03d}" for i in range(1, 39)))
            # A drawn segmenter case stays unsigned until its checked label lands.
            segmenter_ids = frozenset(
                case["id"]
                for case in result.loaded.cases_by_file["build-gates/segmenter-300.jsonl"]
                if isinstance(case["id"], str)
            ) - {label.case_id for label in result.loaded.segmenter_labels}
            self.assertEqual(result.loaded.unsigned_ids, frozenset(research_ids) | segmenter_ids)


def _judgment_case(case_id: str = "judgments-001", question: str = SENTINEL) -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "judgments",
        "category": "judgments",
        "branch": "legal",
        "question": question,
        "labels": ["chu-written", "rewritten"],
        "cluster_id": case_id,
        "notes": "",
        "review": {"by": "CSA-1", "on": "2026-09-19", "accepted_flags": []},
    }


def _triple_case(case_id: str = "judge-001", question: str = SENTINEL) -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "judge",
        "category": "triples",
        "branch": "legal",
        "question": question,
        "expected": {"answer": "The reference answer.", "band": [1, 2]},
        "candidate": "The candidate answer.",
        "labels": ["invented", "partial"],
        "cluster_id": case_id,
        "review": {"by": "CSA-1", "on": "2026-09-19"},
        "notes": "",
    }


def _guardrail_case(
    case_id: str = "deadline-trap/direct-01", question: str = SENTINEL
) -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "guardrails",
        "category": "deadline-trap",
        "branch": "general",
        "question": question,
        "expected": {
            "turn": "blocked",
            "pattern": "deadline/date-near-deadline-word@1",
        },
        "labels": ["invented", "positive"],
        "cluster_id": case_id,
        "review": {"by": "CSA-1", "on": "2026-09-22"},
        "notes": "",
    }


def _tier_2_case() -> dict[str, object]:
    case_id = f"{TIER_2_CATEGORY}/restitution-01"
    return {
        "id": case_id,
        "suite": "guardrails",
        "category": TIER_2_CATEGORY,
        "branch": "general",
        "question": SENTINEL,
        "expected": {"turn": "instructed"},
        "labels": ["invented", "positive"],
        "cluster_id": case_id,
        "review": {"by": "CSA-1", "on": "2026-09-24"},
        "notes": "",
    }


def _superseding(case_id: str, target: str) -> str:
    record = json.loads(_case(case_id))
    notes = record.pop("notes")
    record["supersedes"] = target
    record["notes"] = notes
    return json.dumps(record, separators=(",", ":"))


def _general_smoke_case() -> dict[str, object]:
    return {
        "id": "smoke-01",
        "suite": "general",
        "category": "smoke",
        "branch": "general",
        "question": SENTINEL,
        "expected": {
            "expect": "answered",
            "block": "any",
            "sources": "any",
            "search": False,
            "must": [r"fictional-expected-regex"],
            "must_not": [],
        },
        "labels": ["invented"],
        "cluster_id": "smoke-01",
        "review": {"by": "CSA-1", "on": "2026-09-23"},
        "notes": "",
    }


class CountGateCases(unittest.TestCase):
    """Rule cases have closed ordered fields and supersede within their series."""

    def test_clean_rule_counts_without_a_question_notes_or_signoff(self) -> None:
        case = count_gate_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (case,))
            _write(root, "slices/count-gates/rules.ids", str(case["id"]))
            result = load_set(root)
        self.assertTrue(result.ok, result.findings)
        self.assertIsNotNone(result.loaded)
        loaded = cast(LoadedSet, result.loaded)
        selection = select_cases(loaded, "count-gates")
        self.assertEqual(selection.counted, (case["id"],))
        self.assertEqual(selection.unsigned, ())
        self.assertFalse(selection.takes_signoff)

    def test_shape_refusals_do_not_echo_the_rule(self) -> None:
        changes: tuple[tuple[str, dict[str, object]], ...] = (
            ("case keys or order", {"question": SENTINEL}),
            ("case keys or order", {"notes": ""}),
            ("labels must be spec", {"labels": ["invented"]}),
            ("labels must be spec", {"labels": ["spec", "extra"]}),
            ("labels", {"labels": []}),
            ("rule shape", {"rule": ""}),
            ("rule shape", {"rule": None}),
            ("rule shape", {"rule": " fictional rule"}),
            ("rule shape", {"rule": "fictional rule "}),
            ("rule shape", {"rule": "fictional\nrule"}),
            ("rule shape", {"rule": "fictional\rrule"}),
            ("cluster_id", {"cluster_id": "count-gates-other-01"}),
            ("review.by", {"review": {"by": "wrong", "on": "2099-01-03"}}),
            ("review.on", {"review": {"by": "CSA-1", "on": "2099-02-30"}}),
            ("review keys or order", {"review": {"on": "2099-01-03", "by": "CSA-1"}}),
            ("id pattern", {"id": "count-gates-staged"}),
            ("id pattern", {"id": "count-gates-staged-1"}),
            ("id pattern", {"id": "count-gates-staged-001"}),
            ("id pattern", {"id": "count-gates--staged-01"}),
            ("id pattern", {"id": "count-gates-Staged-01"}),
            ("branch", {"branch": "general"}),
        )
        for rule, fields in changes:
            with self.subTest(rule=rule, fields=fields):
                case = count_gate_case()
                case["rule"] = SENTINEL
                case.update(fields)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "eval-v9"
                    _write_cases(root, (case,))
                    result = load_set(root)
                self.assertIsNone(result.loaded)
                self.assertIn(rule, "\n".join(finding.rule for finding in result.findings))
                self.assertNotIn(SENTINEL, "\n".join(finding.text() for finding in result.findings))
                self.assertTrue(all(finding.fix for finding in result.findings))

    def test_top_level_key_order_is_closed(self) -> None:
        case = count_gate_case()
        case = dict(reversed(tuple(case.items())))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (case,))
            result = load_set(root)
        self.assertIn("case keys or order", tuple(finding.rule for finding in result.findings))

    def test_supersedes_requires_an_earlier_number_in_the_same_series(self) -> None:
        for forward in (True, False):
            with self.subTest(forward=forward), tempfile.TemporaryDirectory() as directory:
                old = count_gate_case()
                new = count_gate_case("count-gates-staged-02")
                successor, predecessor = (new, old) if forward else (old, new)
                successor["supersedes"] = predecessor["id"]
                root = Path(directory) / "eval-v9"
                _write_cases(root, (old, new))
                _write(root, "slices/count-gates/rules.ids", f"{old['id']}\n{new['id']}")
                result = load_set(root)
                if forward:
                    self.assertTrue(result.ok, result.findings)
                    loaded = cast(LoadedSet, result.loaded)
                    self.assertEqual(loaded.active_ids, (new["id"],))
                    self.assertEqual(select_cases(loaded, "count-gates").counted, (new["id"],))
                else:
                    self.assertIsNone(result.loaded)
                    self.assertIn("earlier", "\n".join(finding.rule for finding in result.findings))
class SyntheticCases(unittest.TestCase):
    """Synthetic research cases carry their expected answer and retraced gold."""

    def assert_refused(self, record: dict[str, object], rule: str) -> None:
        findings = case_findings(record, synthetic.case_path("lookup"), 7, ("ca6",))
        self.assertIn(rule, tuple(finding.rule for finding in findings))
        self.assertTrue(all(finding.file == synthetic.case_path("lookup") and finding.line == 7 for finding in findings))
        self.assertTrue(all(finding.fix for finding in findings))
        self.assertNotIn(SENTINEL, "\n".join(finding.text() for finding in findings))

    def test_zero_tolerance_defaults_false_and_marks_five_research_categories(self) -> None:
        self.assertIs(ShapeSpec.__dataclass_fields__["zero_tolerance"].default, False)
        self.assertEqual(
            {key for key, spec in SHAPE_REGISTRY.items() if spec.zero_tolerance},
            {("research-qa", category) for category in
             ("lookup", "edition", "treatment", "section-attribution", "unanswerable")},
        )

    def test_key_declarations_and_origin_specific_review_keys(self) -> None:
        for category in ("lookup", "retrieval", "synthesis", "existence"):
            with self.subTest(category=category):
                spec = SHAPE_REGISTRY[("research-qa", category)]
                self.assertEqual(spec.synthetic_keys, synthetic.CASE_KEYS)
                self.assertIs(spec.synthetic_keys, SHAPE_REGISTRY[("research-qa", "lookup")].synthetic_keys)
                self.assertEqual(spec.question_origins, (synthetic.SYNTHETIC_ORIGIN,) if category == "existence"
                                 else ("harvest", synthetic.SYNTHETIC_ORIGIN))
                self.assertEqual(spec.review_keys, ("by", "on", "accepted_flags"))
                self.assertEqual(spec.review_keys_for("harvest"), spec.review_keys)
                self.assertEqual(spec.review_keys_for(synthetic.SYNTHETIC_ORIGIN),
                                 ("by", "on") if category == "lookup" else ("by", "on", "read"))
                record = _synthetic_case(f"research-qa-{category}-001", category=category)
                self.assertEqual(spec.keys_for(record, synthetic.SYNTHETIC_ORIGIN), synthetic.CASE_KEYS)
                record["supersedes"] = f"research-qa-{category}-000"
                self.assertEqual(spec.keys_for(record, synthetic.SYNTHETIC_ORIGIN), (*synthetic.CASE_KEYS, "supersedes"))
                if category != "existence":
                    harvest = _research_qa_case(category=category)
                    self.assertEqual(spec.keys_for(harvest, "harvest"), tuple(harvest))
        edition = SHAPE_REGISTRY[("research-qa", "edition")]
        self.assertEqual(edition.synthetic_keys, ())
        self.assertEqual(edition.review_keys_for(synthetic.SYNTHETIC_ORIGIN), ("by", "on"))

    def test_existence_shape_has_question_fields_and_takes_no_signoff(self) -> None:
        spec = SHAPE_REGISTRY[("research-qa", "existence")]
        sibling = SHAPE_REGISTRY[("research-qa", "retrieval")]
        self.assertEqual((spec.suite, spec.category), ("research-qa", "existence"))
        self.assertEqual(spec.base_keys, sibling.base_keys)
        self.assertEqual(spec.fixed_fields, {"suite": "research-qa", "category": "existence", "branch": "legal"})
        self.assertIs(spec.id_pattern, sibling.id_pattern)
        self.assertIs(spec.cluster_pattern, sibling.cluster_pattern)
        self.assertEqual(spec.review_keys, sibling.review_keys)
        self.assertEqual(spec.question_origins, (synthetic.SYNTHETIC_ORIGIN,))
        self.assertTrue(spec.is_question_case)
        self.assertIs(spec.takes_signoff, False)

    def test_near_miss_shapes_are_synthetic_only_and_load_without_must_cite(self) -> None:
        lookup = SHAPE_REGISTRY[("research-qa", "lookup")]
        for category in synthetic.NEAR_MISS_CATEGORIES:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                spec = SHAPE_REGISTRY[("research-qa", category)]
                self.assertEqual(spec.question_origins, (synthetic.SYNTHETIC_ORIGIN,))
                self.assertFalse(spec.takes_signoff)
                self.assertEqual(spec.base_keys, lookup.base_keys)
                self.assertIs(spec.id_pattern, lookup.id_pattern)
                self.assertIs(spec.cluster_pattern, lookup.cluster_pattern)
                self.assertIs(spec.synthetic_keys, lookup.synthetic_keys)
                self.assertEqual(spec.review_keys, lookup.review_keys)
                self.assertEqual(spec.zero_tolerance, category == "unanswerable")
                self.assertEqual(spec.expected_keys_for(), ("answer", "behavior", "premise")
                                 if category == "poisoned-premise" else ("answer", "behavior"))
                self.assertEqual(spec.gold_keys_for(), GOLD_COORDINATE_KEYS)
                self.assertEqual(spec.review_keys_for(synthetic.SYNTHETIC_ORIGIN),
                                 ("by", "on") if spec.zero_tolerance else ("by", "on", "read"))
                record = _synthetic_case(category=category)
                relative = synthetic.case_path(category)
                self.assertEqual(case_findings(record, relative, 1, ("ca6",)), ())
                root = Path(directory) / "eval-v9"
                _write(root, relative, synthetic.case_line(record).rstrip("\n"))
                _write(root, "slices/demo/cases.ids", str(record["id"]))
                loaded = load_set(root, ("ca6",))
                self.assertTrue(loaded.ok, loaded.findings)
                assert loaded.loaded is not None
                self.assertEqual(loaded.loaded.unsigned_ids, frozenset())
                self.assertEqual(select_cases(loaded.loaded, "demo").counted, (record["id"],))

    def test_near_miss_expected_and_harvest_refusals_are_located_by_line(self) -> None:
        for category in synthetic.NEAR_MISS_CATEGORIES:
            mutations: list[tuple[str, Callable[[dict[str, object]], object]]] = [
                ("expected keys or order", lambda row: cast(dict[str, object], row["expected"]).update(must_cite=["fictional/source-1"])),
                ("expected.behavior", lambda row: cast(dict[str, object], row["expected"]).update(behavior="invented-wrong-behavior")),
                ("expected.behavior", lambda row: cast(dict[str, object], row["expected"]).pop("behavior")),
            ]
            if category == "poisoned-premise":
                mutations.append(("expected.premise", lambda row: cast(dict[str, object], row["expected"]).pop("premise")))
                for value in (None, 1, "", " ", " Fictitious v. Imaginary", "Fictitious v. Imaginary ",
                              "Fictitious\nv. Imaginary", "Fictitious\rv. Imaginary"):
                    def set_premise(row: dict[str, object], value: object = value) -> None:
                        cast(dict[str, object], row["expected"])["premise"] = value
                    mutations.append(("expected.premise", set_premise))
            for rule, mutate in mutations:
                with self.subTest(category=category, rule=rule), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "eval-v9"
                    relative = synthetic.case_path(category)
                    record = _synthetic_case(category=category)
                    mutate(record)
                    _write(root, relative, synthetic.case_line(record).rstrip("\n"))
                    result = load_set(root, ("ca6",))
                    self.assertFalse(result.ok)
                    self.assertTrue(any(finding.file == relative and finding.line == 1 and finding.rule == rule
                                        for finding in result.findings), result.findings)
                    self.assertTrue(all(finding.fix for finding in result.findings))
                    self.assertNotIn(SENTINEL, "\n".join(finding.text() for finding in result.findings))
            with self.subTest(category=category, origin="harvest"), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "eval-v9"
                relative = synthetic.case_path(category)
                _write(root, relative, json.dumps(_research_qa_case(category=category)))
                result = load_set(root, ("ca6",))
                self.assertTrue(any(finding.file == relative and finding.line == 1 and finding.rule == "labels.origin"
                                    for finding in result.findings), result.findings)

    def test_sampled_categories_load_read_and_unread_cases_as_counted(self) -> None:
        for category in ("retrieval", "synthesis", "existence"):
            for read in (True, False):
                with self.subTest(category=category, read=read), tempfile.TemporaryDirectory() as directory:
                    record = _synthetic_case(f"research-qa-{category}-001", category=category)
                    cast(dict[str, object], record["review"])["read"] = read
                    relative = synthetic.case_path(category)
                    self.assertEqual(case_findings(record, relative, 1, ("ca6",)), ())
                    root = Path(directory) / "eval-v9"
                    _write(root, relative, synthetic.case_line(record).rstrip("\n"))
                    _write(root, "slices/demo/cases.ids", str(record["id"]))
                    result = load_set(root, ("ca6",))
                    self.assertTrue(result.ok, result.findings)
                    assert result.loaded is not None
                    self.assertEqual(result.loaded.cases_by_id[cast(str, record["id"])], record)
                    self.assertEqual(result.loaded.unsigned_ids, frozenset())
                    selection = select_cases(result.loaded, "demo")
                    self.assertEqual(selection.counted, (record["id"],))
                    self.assertEqual(selection.unsigned, ())

    def test_sampled_reviews_require_ordered_boolean_read_and_a_csa(self) -> None:
        value: object
        for category in ("retrieval", "synthesis", "existence"):
            with self.subTest(category=category):
                record = _synthetic_case(f"research-qa-{category}-001", category=category)
                review = cast(dict[str, object], record["review"])
                del review["read"]
                self.assert_refused(record, "review keys or order")
                for value in (None, "true", "false", 0, 1, [], {}):
                    with self.subTest(kind=type(value).__name__):
                        review["read"] = value
                        self.assert_refused(record, "review.read bool")
                review["read"] = True
                review["by"] = "CHU-attorney-1"
                self.assert_refused(record, "review.by role")
                review["by"] = "CSA-1"
                record["review"] = {"read": True, "by": review["by"], "on": review["on"]}
                self.assert_refused(record, "review keys or order")

    def test_existence_refuses_harvest_origin(self) -> None:
        self.assert_refused(_research_qa_case(category="existence"), "labels.origin")

    def test_valid_labels_and_gold_count_code_points_and_keep_paragraph_breaks(self) -> None:
        for labels in ([synthetic.SYNTHETIC_ORIGIN], [synthetic.SYNTHETIC_ORIGIN, synthetic.CLOSED_BOOK_LABEL]):
            with self.subTest(labels=labels):
                record = _synthetic_case()
                record["labels"] = labels
                self.assertEqual(case_findings(record, synthetic.case_path("lookup"), 1, ("ca6",)), ())
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "eval-v9"
                    _write(root, synthetic.case_path("lookup"), synthetic.case_line(record).rstrip("\n"))
                    result = load_set(root, ("ca6",))
                self.assertTrue(result.ok, result.findings)
                assert result.loaded is not None
                self.assertEqual(result.loaded.cases_by_id["research-qa-lookup-001"], record)
                self.assertEqual(result.loaded.unsigned_ids, frozenset())

    def test_every_top_level_key_is_required_and_ordered(self) -> None:
        for key in synthetic.CASE_KEYS:
            with self.subTest(key=key):
                record = _synthetic_case()
                del record[key]
                expected = "unknown suite/category shape" if key in {"suite", "category"} else "case keys or order"
                self.assert_refused(record, expected)
        record = _synthetic_case()
        self.assert_refused(dict(reversed(tuple(record.items()))), "case keys or order")
        record["extra"] = SENTINEL
        self.assert_refused(record, "case keys or order")
        record = _synthetic_case()
        record["supersedes"] = "research-qa-lookup-000"
        self.assertEqual(case_findings(record, synthetic.case_path("lookup"), 1), ())
        reordered = {"supersedes": record.pop("supersedes"), **record}
        self.assert_refused(reordered, "case keys or order")

    def test_ids_labels_templates_and_seed_refusals(self) -> None:
        cases: tuple[tuple[str, str, object], ...] = (
            ("id pattern", "id", "research-qa-001"),
            ("id pattern", "id", "research-qa-lookup-01"),
            ("id pattern", "id", "research-qa-retrieval-001"),
            ("labels", "labels", []),
            ("labels", "labels", "synthetic"),
            ("synthetic labels", "labels", ["synthetic", "verbatim"]),
            ("synthetic labels", "labels", ["synthetic", "closed-book-answerable", "other"]),
            ("synthetic labels", "labels", ["synthetic", "synthetic"]),
            ("labels.origin", "labels", ["closed-book-answerable", "synthetic"]),
            ("labels.origin", "labels", ["invented"]),
            ("template", "template", None),
            ("template", "template", "lookup"),
            ("template", "template", "lookup@0"),
            ("template", "template", "lookup@01"),
            ("template", "template", "retrieval@1"),
            ("template", "template", "lookup@999"),
            ("seed forbidden", "seed", "HARV-001"),
            ("question shape", "question", "Fictitious\nquestion"),
        )
        for rule, key, value in cases:
            with self.subTest(rule=rule, key=key, value=value):
                record = _synthetic_case()
                record[key] = value
                self.assert_refused(record, rule)

    def test_unregistered_synthetic_categories_are_refused_by_origin(self) -> None:
        record = _synthetic_case()
        record["category"] = "edition"
        record["id"] = "research-qa-edition-001"
        self.assert_refused(record, "labels.origin")

    def test_jurisdiction_is_required_and_checked_against_handed_map(self) -> None:
        for value in (None, [], "ca6", [1], [SENTINEL]):
            with self.subTest(kind=type(value).__name__):
                record = _synthetic_case()
                record["jurisdiction"] = value
                self.assert_refused(record, "jurisdiction")
        record = _synthetic_case()
        record["jurisdiction"] = ["fictionalcourt"]
        self.assert_refused(record, "jurisdiction id 'fictionalcourt' is absent from courts.yaml")
        self.assertEqual(case_findings(record, synthetic.case_path("lookup"), 1, ("fictionalcourt",)), ())

    def test_expected_mapping_answer_and_unique_source_id_list(self) -> None:
        value: object
        for value in (None, []):
            record = _synthetic_case()
            record["expected"] = value
            self.assert_refused(record, "expected mapping")
        for key, values, rule in (
            ("answer", ("", " ", None, "Fictitious\nanswer", "Fictitious\ranswer"), "expected.answer"),
            ("must_cite", ([], None, "fictional/source-1", ["fictional/source-1", "fictional/source-1"], ["bad source"], [{}]), "expected.must_cite"),
        ):
            for value in values:
                with self.subTest(key=key, kind=type(value).__name__):
                    record = _synthetic_case()
                    expected = cast(dict[str, object], record["expected"])
                    expected[key] = value
                    self.assert_refused(record, rule)
        for value in ({}, {"must_cite": ["fictional/source-1"], "answer": SENTINEL},
                      {"answer": SENTINEL, "must_cite": ["fictional/source-1"], "extra": SENTINEL}):
            record = _synthetic_case()
            record["expected"] = value
            self.assert_refused(record, "expected keys or order")

    def test_gold_shapes_coordinates_quote_lengths_and_must_cite_coverage(self) -> None:
        value: object
        for value in (None, [], {}):
            record = _synthetic_case()
            record["gold"] = value
            self.assert_refused(record, "gold non-empty list")
        record = _synthetic_case()
        record["gold"] = [None]
        self.assert_refused(record, "gold[1] mapping")
        coordinate_cases = (
            ("source_id", "bad source", "source_id"), ("sha256", "bad hash", "sha256"),
            ("start", -1, "start"), ("start", True, "start"), ("start", "5", "start"),
            ("end", 0, "end"), ("end", False, "end"), ("end", 5, "coordinates"),
            ("quote", None, "quote length"), ("quote", "", "quote length"),
            ("quote", "Wrong fictitious quote", "quote length"),
        )
        for key, value, rule in coordinate_cases:
            with self.subTest(key=key, value=value):
                record = _synthetic_case()
                gold = cast(list[dict[str, object]], record["gold"])
                gold[0][key] = value
                self.assert_refused(record, f"gold[1].{rule}")
        for key in ("source_id", "sha256", "start", "end", "quote"):
            record = _synthetic_case()
            gold = cast(list[dict[str, object]], record["gold"])
            del gold[0][key]
            self.assert_refused(record, "gold[1] keys or order")
        record = _synthetic_case()
        gold = cast(list[dict[str, object]], record["gold"])
        gold[0] = dict(reversed(tuple(gold[0].items())))
        self.assert_refused(record, "gold[1] keys or order")
        record = _synthetic_case()
        gold = cast(list[dict[str, object]], record["gold"])
        gold.append({**gold[0], "source_id": "fictional/source-2"})
        self.assert_refused(record, "gold[2].source_id absent from expected.must_cite")
        expected = cast(dict[str, object], record["expected"])
        expected["must_cite"] = ["fictional/source-1", "fictional/source-2"]
        self.assertEqual(case_findings(record, synthetic.case_path("lookup"), 1), ())
        record["cluster_id"] = "fictional/source-2"
        self.assert_refused(record, "cluster_id must equal first gold source_id")

    def test_reviews_have_only_csa_role_and_date_without_accepted_flags(self) -> None:
        cases = (
            ("review keys or order", {"by": "CSA-1", "on": "2001-01-02", "accepted_flags": []}),
            ("review keys or order", {"by": "CSA-1", "on": "2001-01-02", "read": True}),
            ("review keys or order", {"by": "CSA-1", "on": "2001-01-02", "read": False}),
            ("review keys or order", {"on": "2001-01-02", "by": "CSA-1"}),
            ("review.by role", {"by": "CHU-attorney-1", "on": "2001-01-02"}),
            ("review.by role", {"by": "CSA-0", "on": "2001-01-02"}),
            ("review.on ISO date", {"by": "CSA-1", "on": "2001-02-30"}),
        )
        for rule, review in cases:
            record = _synthetic_case()
            record["review"] = review
            self.assert_refused(record, rule)

    def test_public_case_findings_matches_loader_without_reading_a_set(self) -> None:
        record = _synthetic_case()
        record["template"] = "lookup@999"
        record["question"] = SENTINEL + "\nSecond fictitious line"
        relative = synthetic.case_path("lookup")
        direct = case_findings(record, relative, 1, (court for court in ("ca6",)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, relative, json.dumps(record))
            loaded = load_set(root, ("ca6",))
        self.assertIsNone(loaded.loaded)
        self.assertEqual(set(direct), set(loaded.findings))
        self.assertTrue(all(finding.id == record["id"] for finding in direct))

    def test_synthetic_cases_are_counted_beside_unsigned_harvest_cases(self) -> None:
        first = _synthetic_case()
        successor = _synthetic_case("research-qa-lookup-002")
        successor["supersedes"] = first["id"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_research_cases(root, (_research_qa_case(), first, successor))
            _write(root, "slices/demo/cases.ids", "research-qa-001\nresearch-qa-lookup-001\nresearch-qa-lookup-002")
            result = load_set(root, ("ca6",))
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        self.assertEqual(result.loaded.unsigned_ids, frozenset({"research-qa-001"}))
        selection = select_cases(result.loaded, "demo")
        self.assertEqual(selection.counted, ("research-qa-lookup-002",))
        self.assertEqual(selection.unsigned, ("research-qa-001",))

    def test_signoff_naming_synthetic_case_is_refused_by_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_research_cases(root, (_research_qa_case(), _synthetic_case()))
            _write_signoffs(root, (_signoff(), _signoff("research-qa-lookup-001", answer=SENTINEL)))
            result = load_set(root, ("ca6",))
        self.assertIsNone(result.loaded)
        # The sign-off grammar admits the harvest series alone, so a synthetic
        # id never reaches the relational pass.
        findings = tuple(finding for finding in result.findings if finding.rule == "case_id pattern")
        self.assertEqual(len(findings), 1)
        self.assertEqual((findings[0].file, findings[0].id, findings[0].line), (signoffs.SIGNOFFS_PATH.as_posix(), None, 2))
        self.assertEqual(findings[0].fix, signoffs.SIGNOFF_LINE_FIX)
        self.assertNotIn(SENTINEL, "\n".join(finding.text() for finding in result.findings))

    def test_shape_key_defaults_and_new_synthetic_only_declarations(self) -> None:
        lookup = SHAPE_REGISTRY[("research-qa", "lookup")]
        self.assertEqual(GOLD_COORDINATE_KEYS, tuple(cast(list[dict[str, object]], _synthetic_case()["gold"])[0]))
        self.assertEqual(lookup.expected_keys, ())
        self.assertEqual(lookup.expected_keys_for(), signoffs.EXPECTED_KEYS)
        self.assertEqual(lookup.gold_keys, ())
        self.assertEqual(lookup.gold_keys_for(), GOLD_COORDINATE_KEYS)
        for category, extra in (("treatment", "state"), ("section-attribution", "section_type")):
            with self.subTest(category=category):
                spec = SHAPE_REGISTRY[("research-qa", category)]
                self.assertEqual(spec.question_origins, (synthetic.SYNTHETIC_ORIGIN,))
                self.assertFalse(spec.takes_signoff)
                self.assertIs(spec.synthetic_keys, lookup.synthetic_keys)
                self.assertIs(spec.zero_tolerance, True)
                self.assertEqual(spec.base_keys, lookup.base_keys)
                self.assertIs(spec.id_pattern, lookup.id_pattern)
                self.assertIs(spec.cluster_pattern, lookup.cluster_pattern)
                self.assertEqual(spec.review_keys, lookup.review_keys)
                self.assertEqual(spec.review_keys_for(synthetic.SYNTHETIC_ORIGIN), ("by", "on"))
                self.assertEqual(spec.expected_keys, (*signoffs.EXPECTED_KEYS, extra))
                self.assertEqual(spec.expected_keys_for(), spec.expected_keys)
                if category == "treatment":
                    self.assertEqual(spec.gold_keys, (*GOLD_COORDINATE_KEYS, "citation_id", "signal_id", "pattern_set", "cluster"))
                    self.assertEqual(spec.gold_keys_for(), spec.gold_keys)
                else:
                    self.assertEqual(spec.gold_keys, ())
                    self.assertEqual(spec.gold_keys_for(), GOLD_COORDINATE_KEYS)
                harvest = _research_qa_case(category=category)
                self.assert_refused(harvest, "labels.origin")
                written = _synthetic_case(category=category)
                written["labels"] = ["chu-written", "rewritten"]
                self.assert_refused(written, "labels.origin")

    def test_new_shapes_load_and_count_each_declared_state_and_section_type(self) -> None:
        for category, key, values in (
            ("treatment", "state", (*TREATMENT_STATES, synthetic.NO_STATE)),
            ("section-attribution", "section_type", SECTION_TYPES),
        ):
            records = []
            for index, value in enumerate(values, start=1):
                record = _synthetic_case(f"research-qa-{category}-{index:03d}", category=category)
                cast(dict[str, object], record["expected"])[key] = value
                record["labels"] = [synthetic.SYNTHETIC_ORIGIN, synthetic.CLOSED_BOOK_LABEL]
                self.assertEqual(case_findings(record, synthetic.case_path(category), index, ("ca6",)), ())
                records.append(record)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "eval-v9"
                relative = synthetic.case_path(category)
                _write(root, relative, "".join(synthetic.case_line(record) for record in records).rstrip("\n"))
                _write(root, "slices/demo/cases.ids", "\n".join(str(record["id"]) for record in records))
                result = load_set(root, ("ca6",))
            self.assertTrue(result.ok, result.findings)
            assert result.loaded is not None
            self.assertEqual(result.loaded.cases_by_file[relative], tuple(records))
            self.assertEqual(result.loaded.unsigned_ids, frozenset())
            self.assertEqual(select_cases(result.loaded, "demo").counted, tuple(str(record["id"]) for record in records))

    def test_new_expected_keys_are_exact_ordered_and_not_shared_between_shapes(self) -> None:
        for category in ("treatment", "section-attribution"):
            spec = SHAPE_REGISTRY[("research-qa", category)]
            for key in spec.expected_keys_for():
                record = _synthetic_case(category=category)
                del cast(dict[str, object], record["expected"])[key]
                self.assert_refused(record, "expected keys or order")
            for mutation in ("reverse", "extra", "other-shape"):
                with self.subTest(category=category, mutation=mutation):
                    record = _synthetic_case(category=category)
                    expected = cast(dict[str, object], record["expected"])
                    if mutation == "reverse":
                        record["expected"] = dict(reversed(tuple(expected.items())))
                    elif mutation == "extra":
                        expected["extra"] = SENTINEL
                    else:
                        extra = spec.expected_keys_for()[-1]
                        expected["section_type" if extra == "state" else "state"] = expected.pop(extra)
                    self.assert_refused(record, "expected keys or order")

    def test_new_expected_vocabularies_refuse_wrong_values(self) -> None:
        value: object
        for category, key in (("treatment", "state"), ("section-attribution", "section_type")):
            for value in (None, "", SENTINEL, "NEGATIVE", "good law", 1, True, [], {}):
                with self.subTest(category=category, value=value):
                    record = _synthetic_case(category=category)
                    cast(dict[str, object], record["expected"])[key] = value
                    self.assert_refused(record, f"expected.{key}")

    def test_new_gold_keys_are_exact_and_ordered(self) -> None:
        for category in ("treatment", "section-attribution"):
            spec = SHAPE_REGISTRY[("research-qa", category)]
            for key in spec.gold_keys_for():
                record = _synthetic_case(category=category)
                del cast(list[dict[str, object]], record["gold"])[0][key]
                self.assert_refused(record, "gold[1] keys or order")
            for reverse in (False, True):
                record = _synthetic_case(category=category)
                gold = cast(list[dict[str, object]], record["gold"])
                if reverse:
                    gold[0] = dict(reversed(tuple(gold[0].items())))
                else:
                    gold[0]["extra"] = SENTINEL
                self.assert_refused(record, "gold[1] keys or order")
        for key in ("citation_id", "signal_id", "pattern_set", "cluster"):
            record = _synthetic_case(category="section-attribution")
            cast(list[dict[str, object]], record["gold"])[0][key] = SENTINEL
            self.assert_refused(record, "gold[1] keys or order")

    def test_treatment_gold_edge_field_grammars_and_each_entry(self) -> None:
        value: object
        for key, values in (
            ("citation_id", (None, 1, [], "", "a" * 63, "a" * 65, "A" * 64, "g" * 64)),
            ("signal_id", (None, 1, [], "", "a" * 63, "a" * 65, "A" * 64, "g" * 64)),
            ("pattern_set", (None, 1, [], "", "treatment/patterns@0", "treatment/patterns@01", "fictional/patterns@1")),
            ("cluster", (None, "123", 0, -1, True, False, 1.5, [], {})),
        ):
            for value in values:
                with self.subTest(key=key, value=value):
                    record = _synthetic_case(category="treatment")
                    gold = cast(list[dict[str, object]], record["gold"])
                    gold.append({**gold[0], key: value})
                    self.assert_refused(record, f"gold[2].{key}")
        record = _synthetic_case(category="treatment")
        gold = cast(list[dict[str, object]], record["gold"])
        self.assertRegex(str(gold[0]["pattern_set"]), f"^{PATTERN_SET_PATTERN}$")
        self.assertEqual(case_findings(record, synthetic.case_path("treatment"), 1), ())

    def test_new_shape_refusals_locate_file_id_and_line_in_loader(self) -> None:
        for category, key in (("treatment", "state"), ("section-attribution", "section_type")):
            with self.subTest(category=category):
                record = _synthetic_case(f"research-qa-{category}-002", category=category)
                cast(dict[str, object], record["expected"])[key] = SENTINEL
                relative = synthetic.case_path(category)
                direct = case_findings(record, relative, 2, ("ca6",))
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "eval-v9"
                    _write(root, relative, synthetic.case_line(_synthetic_case(category=category))
                           + synthetic.case_line(record).rstrip("\n"))
                    result = load_set(root, ("ca6",))
                self.assertIsNone(result.loaded)
                self.assertEqual(result.findings, direct)
                self.assertTrue(all(finding.file == relative and finding.id == record["id"] and finding.line == 2
                                    for finding in result.findings))
                self.assertNotIn(SENTINEL, "\n".join(finding.text() for finding in result.findings))


class SegmenterCases(unittest.TestCase):
    """Drawn segmenter cases and checked labels join by id."""

    def test_shape_and_text_sources(self) -> None:
        self.assertEqual(SEGMENTER_TEXT_SOURCES, TEXT_SOURCES)
        case = _segmenter_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (case,))
            result = load_set(root, ("ca6",))
            self.assertTrue(result.ok, result.findings)
            assert result.loaded is not None
            self.assertEqual(result.loaded.unsigned_ids, frozenset({"segmenter-001"}))
            self.assertEqual(result.loaded.segmenter_labels, ())

    def test_shape_refusals(self) -> None:
        changes = (
            ("case keys or order", lambda v: v.update(question=SENTINEL)),
            ("id pattern", lambda v: v.update(id="segmenter-01")),
            ("branch", lambda v: v.update(branch="general")),
            ("opinion_id", lambda v: v.update(opinion_id=0)),
            ("court absent", lambda v: v.update(court="unknown")),
            ("decade", lambda v: v.update(decade=2001)),
            ("text_source", lambda v: v.update(text_source="wrong")),
            ("labels", lambda v: v.update(labels=["other"])),
            ("cluster_id", lambda v: v.update(cluster_id="segmenter-999")),
            ("review.by", lambda v: v.update(review={"by": "wrong", "on": "2026-09-21"})),
        )
        for rule, change in changes:
            with self.subTest(rule=rule):
                case = _segmenter_case()
                change(case)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "eval-v9"
                    _write_cases(root, (case,))
                    result = load_set(root, ("ca6",))
                self.assertIn(rule, "\n".join(finding.rule for finding in result.findings))
                self.assertNotIn(SENTINEL, "\n".join(finding.text() for finding in result.findings))

    def test_labels_file_is_not_a_case_and_moves_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (_segmenter_case(), _segmenter_case("segmenter-002")))
            _write(root, "slices/segmenter-300/sample.ids", "segmenter-001\nsegmenter-002")
            before = load_set(root).loaded
            _write_segmenter_labels(root, (_segmenter_label(),))
            after = load_set(root).loaded
            self.assertIsNotNone(before)
            self.assertIsNotNone(after)
            assert before is not None and after is not None
            self.assertNotEqual(before.digest, after.digest)
            self.assertEqual(after.digest, _digest_model(root))
            self.assertEqual(tuple(after.cases_by_file), ("build-gates/cases.jsonl",))
            self.assertEqual(after.segmenter_labels, (_segmenter_label(),))
            self.assertEqual(after.unsigned_ids, frozenset({"segmenter-002"}))
            selection = select_cases(after, "segmenter-300")
            self.assertEqual(selection.counted, ("segmenter-001",))
            self.assertEqual(selection.unsigned, ("segmenter-002",))
            self.assertTrue(selection.takes_signoff)

    def test_relational_findings_and_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (_segmenter_case(), json.loads(_case("extraction-001"))))
            _write_segmenter_labels(root, (
                _segmenter_label("segmenter-999"),
                _segmenter_label("segmenter-001"),
                _segmenter_label("segmenter-001"),
            ))
            result = load_set(root)
            self.assertEqual(
                tuple((f.line, f.rule) for f in result.findings if f.file == segmenter_labels.LABELS_PATH.as_posix()),
                ((1, "case_id names no case"), (3, "case_id appears more than once")),
            )
            other = _segmenter_case("segmenter-002")
            other["category"] = "other"
            _write_cases(root, (_segmenter_case(), other))
            _write_segmenter_labels(root, (_segmenter_label("segmenter-002"),))
            segmenter_shape = SHAPE_REGISTRY[("build-gates", "segmenter-300")]
            other_shape = ShapeSpec(
                "build-gates", "other", segmenter_shape.base_keys,
                {"suite": "build-gates", "category": "other", "branch": "legal"},
                segmenter_shape.id_pattern, segmenter_shape.cluster_pattern,
                segmenter_shape.review_keys,
            )
            with patch.dict(SHAPE_REGISTRY, {("build-gates", "other"): other_shape}):
                wrong_shape = load_set(root)
            self.assertIn("case_id names a non-segmenter case", tuple(f.rule for f in wrong_shape.findings))

    def test_superseding_correction_keeps_old_label_inert(self) -> None:
        old = _segmenter_case()
        new = _segmenter_case("segmenter-002")
        new["supersedes"] = "segmenter-001"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (old, new))
            _write_segmenter_labels(root, (_segmenter_label(), _segmenter_label("segmenter-002")))
            _write(root, "slices/segmenter-300/sample.ids", "segmenter-001\nsegmenter-002")
            result = load_set(root, ("ca6",))
            self.assertTrue(result.ok, result.findings)
            assert result.loaded is not None
            loaded = result.loaded
            self.assertEqual(loaded.active_ids, ("segmenter-002",))
            self.assertEqual(select_cases(loaded, "segmenter-300").counted, ("segmenter-002",))
            self.assertEqual(len(loaded.segmenter_labels), 2)
            self.assertEqual(loaded.unsigned_ids, frozenset())


class ShapeRefusals(unittest.TestCase):
    """The loader reports each shape and relationship rule without question text."""

    def _assert_case_finding(
        self,
        record: dict[str, object],
        *,
        court_ids: tuple[str, ...] | None = None,
        path: str = "build-gates/cases.jsonl",
        expected: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            suite_path = root / path
            _write(root, path, json.dumps(record, separators=(",", ":")))
            result = load_set(root, court_ids)
            self.assertIsNone(result.loaded)
            rendered = "\n".join(finding.text() for finding in result.findings)
            self.assertIn(suite_path.name, rendered)
            self.assertIn(str(record["id"]), rendered)
            self.assertIn(expected, rendered)
            self.assertNotIn(SENTINEL, rendered)

    def _assert_relational_finding(
        self,
        records: tuple[dict[str, object], ...],
        *,
        case_id: str,
        expected: str,
    ) -> None:
        """A relational rule needs the whole file, so the set is planted, not one case."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, records)
            result = load_set(root)
            self.assertIsNone(result.loaded)
            rendered = "\n".join(finding.text() for finding in result.findings)
            self.assertIn(f"cases.jsonl:id {case_id}", rendered)
            self.assertIn(expected, rendered)
            self.assertNotIn(SENTINEL, rendered)

    def test_version_directory_must_be_eval_vn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-test"
            result = load_set(root)
            self.assertIsNone(result.loaded)
            self.assertTrue(any("eval-vN" in finding.rule for finding in result.findings))

    def test_shape_registry_rejects_unknown_shape_and_wrong_suite_directory(self) -> None:
        record = json.loads(_case("extraction-001"))
        record["category"] = "unknown"
        self._assert_case_finding(record, expected="unknown suite/category")
        self._assert_case_finding(
            json.loads(_case("extraction-001")), path="other/cases.jsonl", expected="directory"
        )

    def test_extraction_shape_rejects_keys_fixed_fields_and_id(self) -> None:
        cases = [
            ("case keys", lambda value: value.update(extra=True)),
            ("unknown suite/category", lambda value: value.update(suite="wrong")),
            ("unknown suite/category", lambda value: value.update(category="wrong")),
            ("branch", lambda value: value.update(branch="civil")),
            ("id pattern", lambda value: value.update(id="not-an-extraction-id")),
        ]
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                record = json.loads(_case("extraction-001"))
                mutate(record)
                self._assert_case_finding(record, expected=expected)

    def test_triple_shape_rejects_expected_answer_band_candidate_and_labels(self) -> None:
        cases: list[tuple[str, object]] = [
            ("expected keys", {"answer": "The reference answer."}),
            ("expected.answer", {"answer": "", "band": [1, 2]}),
            ("expected.band", {"answer": "The reference answer.", "band": [3, 1]}),
            ("candidate", ""),
            ("labels", ["invented"]),
            ("labels", ["not-invented", "faithful"]),
            ("labels", ["invented", "not-a-kind"]),
        ]
        for expected, value in cases:
            with self.subTest(expected=expected):
                record = _triple_case()
                if expected.startswith("expected"):
                    record["expected"] = value
                elif expected == "candidate":
                    record["candidate"] = value
                else:
                    record["labels"] = value
                self._assert_case_finding(
                    record,
                    path="judge/triples.jsonl",
                    expected=expected,
                )

    def test_clean_triple_shape_loads_without_extraction_object_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(
                root,
                "judge/triples.jsonl",
                json.dumps(_triple_case(), separators=(",", ":")),
            )
            _write(root, "slices/judge-triples/triples.ids", "judge-001")
            result = load_set(root)
            self.assertTrue(result.ok, result.findings)

    def test_extraction_shape_rejects_question_labels_seed_cluster_review_notes_and_supersedes(self) -> None:
        cases: list[tuple[str, str, object]] = [
            ("question shape", "question", " " + SENTINEL),
            ("labels origin", "labels", ["wrong"]),
            ("harvest labels", "labels", ["harvest"]),
            ("seed", "seed", "bad"),
            ("cluster_id", "cluster_id", "bad"),
            ("review keys", "review", {"on": "2026-09-19", "by": "CSA-1"}),
            ("review.by", "review", {"by": "bad", "on": "2026-09-19"}),
            ("review.on", "review", {"by": "CSA-1", "on": "not-a-date"}),
            ("notes", "notes", None),
            ("supersedes string", "supersedes", 1),
        ]
        for expected, field, value in cases:
            with self.subTest(expected=expected):
                record = json.loads(_case("extraction-001"))
                record[field] = value
                self._assert_case_finding(record, expected=expected)

    def test_extraction_harvest_seed_and_cluster_rules(self) -> None:
        for field, value, expected in (
            ("seed", "HARV-nope", "seed"),
            ("cluster_id", "harvest-chat-NOT-LOWERCASE-HEX", "cluster_id"),
        ):
            with self.subTest(field=field):
                record = _harvest_extraction_case()
                record[field] = value
                self._assert_case_finding(record, expected=expected)
        record = json.loads(_case("extraction-001"))
        record["labels"] = ["invented", "doctrinal"]
        self._assert_case_finding(record, expected="invented labels")
        harvest = _harvest_extraction_case()
        review = harvest.pop("review")
        harvest["review"] = review
        self._assert_case_finding(harvest, expected="case keys or order")

    def test_variant_shape_rejects_key_order_labels_seed_and_invented_parent(self) -> None:
        # parent belongs between labels and cluster_id; re-inserting it puts it last.
        record = _variant_case()
        record["parent"] = record.pop("parent")
        self._assert_case_finding(record, expected="case keys or order")

        for labels in (["variant"], ["variant", "bad axis"]):
            with self.subTest(labels=labels):
                record = _variant_case()
                record["labels"] = labels
                self._assert_case_finding(record, expected="variant labels")

        record = _variant_case()
        record["seed"] = "HARV-001"
        self._assert_case_finding(record, expected="seed forbidden")

        record = _variant_case()
        record["parent"] = 1
        self._assert_case_finding(record, expected="parent")

        invented = json.loads(_case("extraction-001"))
        invented["parent"] = "extraction-000"
        self._assert_case_finding(invented, expected="case keys or order")

    def test_variant_parent_relationships_are_refused(self) -> None:
        self._assert_case_finding(
            _variant_case(parent="extraction-999"), expected="parent names no case"
        )

        self._assert_relational_finding(
            (
                _variant_case(case_id="extraction-001", parent="extraction-002"),
                json.loads(_case("extraction-002")),
            ),
            case_id="extraction-001",
            expected="parent names no earlier case of its id series",
        )
        self._assert_relational_finding(
            (
                json.loads(_case("extraction-001")),
                _variant_case(),
                _variant_case(case_id="extraction-003", parent="extraction-002"),
            ),
            case_id="extraction-003",
            expected="parent names a variant",
        )
        self._assert_relational_finding(
            (json.loads(_case("extraction-001")), _variant_case(cluster_id="other-cluster")),
            case_id="extraction-002",
            expected="cluster_id differs from the parent's",
        )

    def test_valid_variant_over_valid_parent_loads_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (json.loads(_case("extraction-001")), _variant_case()))
            result = load_set(root)
            self.assertTrue(result.ok, result.findings)

    def test_active_ids_match_scorer_for_a_parent_chain(self) -> None:
        # A superseded parent, its variant, the successor, and the successor's variant.
        records = (
            json.loads(_case("extraction-001")),
            _variant_case(),
            json.loads(_superseding("extraction-003", "extraction-001")),
            _variant_case(case_id="extraction-004", parent="extraction-003"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, records)
            result = load_set(root)
            self.assertTrue(result.ok, result.findings)
            assert result.loaded is not None
            self.assertEqual(result.loaded.active_ids, ("extraction-003", "extraction-004"))
            self.assertEqual(result.loaded.active_ids, _scorer_active_ids(result.loaded))

    def test_committed_active_ids_match_scorer(self) -> None:
        court_map = load_court_map(ROOT / "courts.yaml").court_map
        assert court_map is not None
        result = load_set(ROOT / SET_ROOT, court_map.courts)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        self.assertEqual(result.loaded.active_ids, _scorer_active_ids(result.loaded))

    def test_judgment_shape_rejects_fields_and_review_contract(self) -> None:
        cases = [
            ("keys", lambda value: value.update(extra=True)),
            ("id pattern", lambda value: value.update(id="judgments-01")),
            ("labels.origin", lambda value: value.update(labels=["wrong", "rewritten"])),
            ("labels.wording", lambda value: value.update(labels=["chu-written", "wrong"])),
            ("seed forbidden", lambda value: value.update(seed="HARV-001")),
            ("cluster_id", lambda value: value.update(cluster_id="other")),
            ("notes", lambda value: value.update(notes=None)),
            ("review keys", lambda value: value.update(review={"by": "CSA-1", "on": "2026-09-19"})),
            ("review.accepted_flags", lambda value: value.update(review={"by": "CSA-1", "on": "2026-09-19", "accepted_flags": ["b", "a"]})),
            ("question shape", lambda value: value.update(question=" " + SENTINEL)),
        ]
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                record = _judgment_case()
                mutate(record)
                self._assert_case_finding(record, path="judgments/queries.jsonl", expected=expected)

    def test_research_qa_shapes_reject_the_judgment_rules_and_expected(self) -> None:
        cases = [
            ("keys", lambda value: value.update(extra=True)),
            ("id pattern", lambda value: value.update(id="research-qa-01")),
            ("labels.origin", lambda value: value.update(labels=["wrong", "rewritten", "other"])),
            ("labels.wording", lambda value: value.update(labels=["harvest", "wrong", "other"])),
            ("seed", lambda value: value.update(seed="HARV-nope")),
            ("cluster_id", lambda value: value.update(cluster_id="other")),
            ("notes", lambda value: value.update(notes=None)),
            ("review keys", lambda value: value.update(review={"by": "CSA-1", "on": "2026-09-21"})),
            ("review.accepted_flags", lambda value: value.update(review={"by": "CSA-1", "on": "2026-09-21", "accepted_flags": ["b", "a"]})),
            ("question shape", lambda value: value.update(question=" " + SENTINEL)),
            ("case keys", lambda value: value.update(expected={"answer": "not allowed"})),
        ]
        for category in ("retrieval", "synthesis", "lookup", "edition"):
            for expected, mutate in cases:
                with self.subTest(category=category, expected=expected):
                    record = _research_qa_case(category=category)
                    mutate(record)
                    self._assert_case_finding(
                        record,
                        path="research-qa/cases.jsonl",
                        expected=expected,
                    )

    def test_guardrail_shapes_reject_bad_ids_expected_values_and_roles(self) -> None:
        cases = [
            ("id pattern", lambda value: value.update(id="deadline-trap/direct-1")),
            (
                "id category prefix",
                lambda value: value.update(id="guidelines-range/direct-01"),
            ),
            ("expected keys or order", lambda value: value.update(expected={"pattern": "x", "turn": "blocked"})),
            ("expected.turn role", lambda value: value.update(expected={"turn": "declined", "pattern": "deadline/date-near-deadline-word@1"})),
            ("expected.turn role", lambda value: value.update(expected={"turn": "clean", "pattern": "deadline/date-near-deadline-word@1"})),
            ("expected.pattern", lambda value: value.update(expected={"turn": "blocked", "pattern": "not-a-pattern"})),
            (
                "expected keys or order",
                lambda value: value.update(
                    expected={
                        "turn": "blocked",
                        "must_not": "forbidden",
                        "pattern": "deadline/date-near-deadline-word@1",
                    }
                ),
            ),
            (
                "expected.must_not",
                lambda value: value.update(expected={"turn": "blocked", "pattern": "deadline/date-near-deadline-word@1", "must_not": ""}),
            ),
            (
                "expected.must_not",
                lambda value: value.update(expected={"turn": "blocked", "pattern": "deadline/date-near-deadline-word@1", "must_not": []}),
            ),
            (
                "expected.must_not",
                lambda value: value.update(expected={"turn": "blocked", "pattern": "deadline/date-near-deadline-word@1", "must_not": 7}),
            ),
            (
                "expected.must_not",
                lambda value: value.update(expected={"turn": "blocked", "pattern": "deadline/date-near-deadline-word@1", "must_not": ["forbidden", 7]}),
            ),
            (
                "expected.must_not",
                lambda value: value.update(expected={"turn": "blocked", "pattern": "deadline/date-near-deadline-word@1", "must_not": [""]}),
            ),
            (
                "expected.must_not",
                lambda value: value.update(expected={"turn": "blocked", "pattern": "deadline/date-near-deadline-word@1", "must_not": "["}),
            ),
            ("labels", lambda value: value.update(labels=["invented", "other"])),
            ("labels", lambda value: value.update(labels=["harvest", "positive"])),
            ("branch must be 'general'", lambda value: value.update(branch="legal")),
        ]
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                record = _guardrail_case()
                mutate(record)
                self._assert_case_finding(
                    record,
                    path="guardrails/deadline-trap.jsonl",
                    expected=expected,
                )

        control = _guardrail_case("deadline-trap/control-01")
        control["expected"] = {"turn": "clean", "pattern": "deadline/date-near-deadline-word@1"}
        control["labels"] = ["invented", "control"]
        self._assert_case_finding(
            control,
            path="guardrails/deadline-trap.jsonl",
            expected="expected keys or order",
        )
        control["expected"] = {"turn": "blocked"}
        self._assert_case_finding(
            control,
            path="guardrails/deadline-trap.jsonl",
            expected="expected.turn role",
        )
        control["expected"] = {"turn": "clean", "must_not": "forbidden"}
        self._assert_case_finding(
            control,
            path="guardrails/deadline-trap.jsonl",
            expected="expected keys or order",
        )

    def test_tier_two_shape_rejects_family_fields_turns_and_control_role(self) -> None:
        for extra in ({"pattern": "deadline/example@1"}, {"must_not": "fictional"}):
            with self.subTest(extra=extra):
                record = _tier_2_case()
                cast(dict[str, object], record["expected"]).update(extra)
                self._assert_case_finding(
                    record,
                    path=f"guardrails/{TIER_2_CATEGORY}.jsonl",
                    expected="expected keys or order",
                )

        wrong_turn = _tier_2_case()
        wrong_turn["expected"] = {"turn": "blocked"}
        self._assert_case_finding(
            wrong_turn,
            path=f"guardrails/{TIER_2_CATEGORY}.jsonl",
            expected="expected.turn role",
        )

        control = _tier_2_case()
        control["labels"] = ["invented", "control"]
        self._assert_case_finding(
            control,
            path=f"guardrails/{TIER_2_CATEGORY}.jsonl",
            expected="labels",
        )

    def test_family_shape_rejects_instructed_turn_and_id_pattern_accepts_digit_category(self) -> None:
        family = _guardrail_case()
        family["expected"] = {
            "turn": "instructed",
            "pattern": "deadline/date-near-deadline-word@1",
        }
        self._assert_case_finding(
            family,
            path="guardrails/deadline-trap.jsonl",
            expected="expected.turn role",
        )
        self.assertIsNotNone(
            GUARDRAIL_ID_PATTERN.fullmatch(f"{TIER_2_CATEGORY}/restitution-01")
        )

    def test_guardrail_positive_must_not_string_and_list_load_clean(self) -> None:
        for index, must_not in enumerate(("forbidden", ["forbidden", r"secret\s+figure"]), start=1):
            with self.subTest(form=type(must_not).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "eval-v9"
                record = _guardrail_case(f"deadline-trap/direct-{index:02d}")
                record["expected"] = {
                    "turn": "blocked",
                    "pattern": "deadline/date-near-deadline-word@1",
                    "must_not": must_not,
                }
                _write(
                    root,
                    "guardrails/deadline-trap.jsonl",
                    json.dumps(record, separators=(",", ":")),
                )
                result = load_set(root)
            self.assertTrue(result.ok, result.findings)

    def test_guardrail_must_not_findings_do_not_quote_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            record = _guardrail_case()
            record["expected"] = {
                "turn": "blocked",
                "pattern": "deadline/date-near-deadline-word@1",
                "must_not": "fictional-sensitive-pattern[",
            }
            _write(
                root,
                "guardrails/deadline-trap.jsonl",
                json.dumps(record, separators=(",", ":")),
            )
            result = load_set(root)
        self.assertIsNone(result.loaded)
        rendered = "\n".join(finding.text() for finding in result.findings)
        self.assertIn("expected.must_not", rendered)
        self.assertIn("deadline-trap/direct-01", rendered)
        self.assertNotIn("fictional-sensitive-pattern", rendered)
        self.assertNotIn(SENTINEL, rendered)

    def test_judgment_fixed_fields_and_minimum_labels_are_checked(self) -> None:
        for field, value, expected in (
            ("suite", "wrong", "unknown suite/category"),
            ("category", "wrong", "unknown suite/category"),
            ("branch", "civil", "branch must be 'legal'"),
        ):
            with self.subTest(field=field):
                record = _judgment_case()
                record[field] = value
                self._assert_case_finding(record, path="judgments/queries.jsonl", expected=expected)
        record = _judgment_case()
        record["labels"] = ["chu-written"]
        self._assert_case_finding(record, path="judgments/queries.jsonl", expected="labels")

    def test_judgment_optional_dates_jurisdiction_and_supersedes_are_checked(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = [
            ("reference_date", {"reference_date": "bad"}),
            ("jurisdiction", {"jurisdiction": []}),
            ("supersedes string", {"supersedes": 1}),
        ]
        for expected, additions in cases:
            with self.subTest(expected=expected):
                record = _judgment_case()
                record.update(additions)
                self._assert_case_finding(record, path="judgments/queries.jsonl", expected=expected)

    def test_judgment_harvest_seed_cluster_and_query_type_rules(self) -> None:
        for field, value, expected in (
            ("seed", "HARV-nope", "seed"),
            ("cluster_id", "harvest-chat-bad space", "cluster_id"),
        ):
            with self.subTest(field=field):
                record: dict[str, object] = {
                    "id": "judgments-001",
                    "suite": "judgments",
                    "category": "judgments",
                    "branch": "legal",
                    "question": SENTINEL,
                    "labels": ["harvest", "rewritten", "doctrinal"],
                    "seed": "HARV-001",
                    "cluster_id": "harvest-chat-abcdef",
                    "notes": "",
                    "review": {"by": "CSA-1", "on": "2026-09-19", "accepted_flags": []},
                }
                record[field] = value
                self._assert_case_finding(record, path="judgments/queries.jsonl", expected=expected)
        query_type_record: dict[str, object] = {
            "id": "judgments-001",
            "suite": "judgments",
            "category": "judgments",
            "branch": "legal",
            "question": SENTINEL,
            "labels": ["harvest", "rewritten", "not-a-query-type"],
            "seed": "HARV-001",
            "cluster_id": "harvest-chat-abcdef",
            "notes": "",
            "review": {"by": "CSA-1", "on": "2026-09-19", "accepted_flags": []},
        }
        self._assert_case_finding(query_type_record, path="judgments/queries.jsonl", expected="labels.query_type")

    def test_judgment_harvest_optional_keys_have_the_declared_order(self) -> None:
        record = {
            "id": "judgments-001",
            "suite": "judgments",
            "category": "judgments",
            "branch": "legal",
            "question": SENTINEL,
            "reference_date": "2026-09-19",
            "jurisdiction": ["ca1"],
            "labels": ["harvest", "rewritten", "doctrinal"],
            "seed": "HARV-001",
            "cluster_id": "harvest-chat-abcdef",
            "notes": "",
            "review": {"by": "CSA-1", "on": "2026-09-19", "accepted_flags": []},
        }
        wrong_order = dict(record)
        jurisdiction = wrong_order.pop("jurisdiction")
        wrong_order["jurisdiction"] = jurisdiction
        self._assert_case_finding(
            {**wrong_order}, path="judgments/queries.jsonl", expected="case keys or order"
        )

    def test_judgment_question_cannot_contain_a_newline(self) -> None:
        record = _judgment_case(question=SENTINEL + "\ncontinued")
        self._assert_case_finding(record, path="judgments/queries.jsonl", expected="question shape")

    def test_jurisdiction_ids_must_be_in_the_handed_court_map(self) -> None:
        record = _judgment_case()
        record["jurisdiction"] = ["nd9"]
        self._assert_case_finding(
            record,
            court_ids=("ca1",),
            path="judgments/queries.jsonl",
            expected="absent from courts.yaml",
        )

    def test_supersedes_requires_an_existing_earlier_unique_target(self) -> None:
        cases = (
            ("names no case", "extraction-002", "extraction-009", ()),
            ("no earlier case", "extraction-001", "extraction-002", ()),
            ("already supersedes", "extraction-003", "extraction-001", ("extraction-002",)),
        )
        for expected, case_id, target, others in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "eval-v9"
                _write(root, "build-gates/a.jsonl", _superseding(case_id, target))
                lines = [_case("extraction-001"), _case("extraction-002")]
                lines = [line for line in lines if json.loads(line)["id"] != case_id]
                lines = [
                    _superseding(json.loads(line)["id"], target) if json.loads(line)["id"] in others else line
                    for line in lines
                ]
                _write(root, "build-gates/b.jsonl", "\n".join(lines))
                findings = [finding for finding in load_set(root).findings if expected in finding.rule]
                self.assertEqual([(finding.file, finding.id) for finding in findings], [("build-gates/a.jsonl", case_id)])

    def test_extraction_expected_objects_must_obey_the_contract(self) -> None:
        cases = [
            ("expected mapping", {"expected": []}),
            ("expected.objects list", {"expected": {"objects": {}}}),
            ("closed type", {"expected": {"objects": [{"type": "unknown"}]}}),
            (
                "keys or order",
                {"expected": {"objects": [{"start": 0, "type": "docket", "end": 1, "text": "["}]}},
            ),
            (
                "contract key invariant",
                {"expected": {"objects": [{"type": "statute", "start": 0, "end": 1, "text": "[", "subsections": []}]}},
            ),
            (
                "contract key invariant",
                {"expected": {"objects": [{"type": "docket", "start": 0, "end": 1, "text": "[", "key": "not-allowed"}]}},
            ),
            (
                "keys or order",
                {"expected": {"objects": [{"type": "bare_section", "start": 0, "end": 1, "text": "["}]}},
            ),
            (
                "keys or order",
                {"expected": {"objects": [{"type": "docket", "start": 0, "end": 1, "text": "[", "subsections": []}]}},
            ),
            (
                "field types",
                {"expected": {"objects": [{"type": "docket", "start": "0", "end": 1, "text": "["}]}},
            ),
            (
                "contract span invariant",
                {"expected": {"objects": [{"type": "docket", "start": 9, "end": 2, "text": "x"}]}},
            ),
            (
                "contract span invariant",
                {"expected": {"objects": [{"type": "docket", "start": 0, "end": 1, "text": "x"}]}},
            ),
            (
                "contract span invariant",
                {"expected": {"objects": [{"type": "docket", "start": 0, "end": 999, "text": SENTINEL}]}},
            ),
            (
                "contract key invariant",
                {"expected": {"objects": [{"type": "statute", "start": 0, "end": 1, "text": "["}]}},
            ),
            (
                "contract ordering invariant",
                {
                    "expected": {
                        "objects": [
                            {"type": "docket", "start": 0, "end": 1, "text": "["},
                            {"type": "docket", "start": 0, "end": 2, "text": "[F"},
                        ]
                    }
                },
            ),
        ]
        for expected, update in cases:
            with self.subTest(expected=expected):
                record = json.loads(_case("extraction-001"))
                record.update(update)
                self._assert_case_finding(record, expected=expected)

    def test_general_smoke_rejects_id_labels_and_expected_key_order(self) -> None:
        cases = [
            ("id pattern", lambda record: record.update(id="Smoke-1")),
            ("labels", lambda record: record.update(labels=["invented", "control"])),
            (
                "expected keys or order",
                lambda record: cast(dict[str, object], record["expected"]).update(
                    extra="fictional"
                ),
            ),
            (
                "expected keys or order",
                lambda record: record.update(
                    expected=dict(
                        reversed(tuple(cast(dict[str, object], record["expected"]).items()))
                    )
                ),
            ),
        ]
        for rule, change in cases:
            with self.subTest(rule=rule):
                record = _general_smoke_case()
                change(record)
                self._assert_case_finding(
                    record, path="general/smoke.jsonl", expected=rule
                )

    def test_general_smoke_rejects_values_outside_harness_vocabularies(self) -> None:
        cases = (
            ("expected.expect", "expect", "fictional-expectation"),
            ("expected.block", "block", "fictional-presence"),
            ("expected.sources", "sources", "fictional-presence"),
        )
        for rule, field, value in cases:
            with self.subTest(field=field):
                record = _general_smoke_case()
                cast(dict[str, object], record["expected"])[field] = value
                self._assert_case_finding(
                    record, path="general/smoke.jsonl", expected=rule
                )

    def test_general_smoke_requires_boolean_search_and_compiling_regex_lists(self) -> None:
        cases = (
            ("expected.search", "search", "false"),
            ("expected.must regex", "must", ["("]),
            ("expected.must_not regex", "must_not", ["("]),
            ("expected.must", "must", "not-a-list"),
        )
        for rule, field, value in cases:
            with self.subTest(field=field, rule=rule):
                record = _general_smoke_case()
                cast(dict[str, object], record["expected"])[field] = value
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "eval-v9"
                    path = "general/smoke.jsonl"
                    _write(root, path, json.dumps(record, separators=(",", ":")))
                    result = load_set(root)
                    self.assertIsNone(result.loaded)
                    rendered = "\n".join(finding.text() for finding in result.findings)
                    self.assertIn("smoke.jsonl:id smoke-01", rendered)
                    self.assertIn(rule, rendered)
                    self.assertNotIn(SENTINEL, rendered)
                    self.assertNotIn("fictional-expected-regex", rendered)
                    self.assertNotIn('"("', rendered)


if __name__ == "__main__":
    unittest.main()
