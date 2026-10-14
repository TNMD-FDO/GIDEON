"""Pure section-overlap and segmenter gate contracts."""

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple, cast
from unittest.mock import patch

from test_evaluation_run import ROOT, EvalHost, _invoke, _run_kwargs

from gideon.evaluation.evalset import SEGMENTER_TEXT_SOURCES, LoadedSet
from gideon.evaluation.results import ImageAccess, RunContext
from gideon.evaluation.segmenter_labels import LabelSpan, SegmenterLabel
from gideon.evaluation.segmenter_slice import (
    COURT_TYPES,
    PRECISION_FLOOR,
    PROBLEMS,
    SEPARATE_TYPES,
    UNKNOWN_CEILING,
    CaseFigures,
    case_figures,
    overlap_matrix,
    page_locator_count,
    record_agrees,
    run_segmenter,
    slice_gate,
)
from gideon.host import cas
from gideon.host.render.worker import SECTION_TYPES
from gideon.host.sysio import Host, PathLike

TEXT = "Fictional majority text" + "Dissent"
TEXT_HASH = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
DEFAULT_SECTIONS = [[0, len(TEXT) - 7, "majority", "row"], [len(TEXT) - 7, len(TEXT), "dissent", "row"]]


def _label(case_id: str = "segmenter-001", *, sha256: str = TEXT_HASH) -> SegmenterLabel:
    return SegmenterLabel(
        case_id, sha256, len(TEXT),
        (LabelSpan(0, len(TEXT) - 7, "majority"), LabelSpan(len(TEXT) - 7, len(TEXT), "dissent")),
        "CSA-1", "2026-09-21",
    )


def _case(case_id: str, opinion_id: int) -> dict[str, object]:
    return {
        "id": case_id, "suite": "build-gates", "category": "segmenter-300",
        "branch": "legal", "opinion_id": opinion_id, "court": "ca6", "decade": 2000,
        "text_source": "plain_text", "labels": ["drawn"], "cluster_id": case_id,
        "review": {"by": "CSA-1", "on": "2026-09-21"}, "notes": "",
    }


def _record_document(opinion_id: int, *, sections: list[list[object]] | None = None, canonical: str = TEXT_HASH) -> dict[str, object]:
    return {
        "opinion_id": opinion_id, "doc_id": "b" * 64,
        "text_source": "plain_text", "sha256": TEXT_HASH,
        "canonical_sha256": canonical, "opinion_type": "010combined",
        "sections": DEFAULT_SECTIONS if sections is None else sections,
    }


class SegmenterHost(EvalHost):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[dict[str, object]] = [_record_document(101)]
        self.originals: dict[str, bytes] = {TEXT_HASH: TEXT.encode("utf-8")}
        self.leg_sections: list[list[object]] = DEFAULT_SECTIONS
        self.leg_texts: dict[str, str] = {}
        self.leg_markers: dict[str, list[list[object]]] = {}
        self.pgmap_counts: dict[str, tuple[int, int]] = {}
        self.read_rc = 0
        self.read_stdout: str | None = None
        self.leg_rc = 0
        self.leg_stdout: str | None = None
        self.leg_input: str | None = None

    def run(
        self, argv: tuple[str, ...] | list[str], *, check: bool = False,
        input: str | None = None, cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command_argv = tuple(argv)
        if input is not None and "\\set opinion_ids " in input:
            self.calls.append((command_argv, input))
            return subprocess.CompletedProcess(list(argv), self.read_rc, self.read_stdout if self.read_stdout is not None else json.dumps(self.documents), "private record diagnostic")
        if command_argv[:2] == ("docker", "run") and command_argv[-1] == "gideon.evaluation.segmenter_leg":
            self.calls.append((command_argv, input))
            self.leg_input = input
            requests = tuple(json.loads(line) for line in (input or "").splitlines())
            replies: list[str] = []
            for item in requests:
                text = self.leg_texts.get(item["id"], item["original"])
                checked, disagreeing = self.pgmap_counts.get(item["id"], (0, 0))
                replies.append(json.dumps({
                    "id": item["id"], "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "length": len(text), "sections": self.leg_sections,
                    "markers": self.leg_markers.get(item["id"], []),
                    "pgmap_checked": checked, "pgmap_disagreeing": disagreeing,
                }) + "\n")
            reply = "".join(replies)
            return subprocess.CompletedProcess(list(argv), self.leg_rc, self.leg_stdout if self.leg_stdout is not None else reply, "private image diagnostic")
        return super().run(argv, check=check, input=input, cwd=cwd, env=env, timeout=timeout, passthrough=passthrough)

    def stat(self, path: PathLike) -> os.stat_result:
        if Path(path) == cas.ROOT:
            return os.stat_result((stat.S_IFDIR | 0o770, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        return super().stat(path)

    def read_bytes(self, path: PathLike) -> bytes:
        name = Path(path).name
        if name not in self.originals:
            raise FileNotFoundError(path)
        return self.originals[name]


def _loaded(*, label_hash: str = TEXT_HASH, second: bool = False, unsigned: bool = False) -> LoadedSet:
    cases = [_case("segmenter-001", 101)]
    if second:
        cases.append(_case("segmenter-002", 102))
    ids = tuple(cast(str, case["id"]) for case in cases)
    labels: tuple[SegmenterLabel, ...] = (_label(sha256=label_hash),)
    if second and not unsigned:
        labels += (_label("segmenter-002"),)
    return LoadedSet(
        "eval-v9", {"build-gates/cases.jsonl": tuple(cases)},
        {cast(str, case["id"]): case for case in cases}, ids,
        {"segmenter-300": ids}, {}, "fictitious-digest",
        segmenter_labels=labels,
        unsigned_ids=frozenset({"segmenter-002"}) if unsigned else frozenset(),
    )


def _context(host: SegmenterHost, *, image: bool = True, turns_dir: str = "/tmp/segmenter-rendered") -> RunContext:
    return RunContext(
        cast(Host, host), turns_dir, turns_dir, None, None, 1, lambda _line: None,
        image=ImageAccess("registry.example/gideon@sha256:" + "e" * 64, ROOT) if image else None,
    )


def _markup_fixture(
    host: SegmenterHost, loaded: LoadedSet, case_id: str, original: str,
    *, column: str = "xml_harvard",
) -> None:
    """Serve fictitious markup whose canonical text is the checked TEXT fixture."""

    case = loaded.cases_by_id[case_id]
    case["text_source"] = column
    document = next(item for item in host.documents if item["opinion_id"] == case["opinion_id"])
    raw = original.encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()
    host.originals[sha256] = raw
    document.update(text_source=column, sha256=sha256)
    host.leg_texts[case_id] = TEXT


class StoredSection(NamedTuple):
    char_start: int
    char_end: int
    section_type: str
    typed_by: str


class PageLocators(unittest.TestCase):
    """A second parser counts each page-locator shape the opinion walk marks."""

    def test_start_and_self_closing_shapes_match_the_walk(self) -> None:
        from gideon.worker import opiniontext

        shapes = (
            ("page-number", ""),
            ("span", ' class="other star-pagination trailing"'),
            ("span", ' class="other ldml-pagenumber trailing"'),
            ("a", ' class="other page-label trailing"'),
        )
        for column in SEGMENTER_TEXT_SOURCES:
            if column == "plain_text":
                continue
            for tag, attrs in shapes:
                for self_closing in (False, True):
                    with self.subTest(column=column, tag=tag, attrs=attrs, self_closing=self_closing):
                        locator = f"<{tag}{attrs}/>" if self_closing else f"<{tag}{attrs}>*12</{tag}>"
                        original = f"<root>{locator}<p>{TEXT}</p></root>"
                        count = page_locator_count(column, original)
                        self.assertEqual(count, 1)
                        parsed = opiniontext.canonical_text(column, original)
                        self.assertEqual(count, sum(item.kind == "page" for item in parsed.markers))

    def test_upper_case_names_and_non_locator_classes(self) -> None:
        fixtures = (
            ('<PAGE-NUMBER>*12</PAGE-NUMBER>', 1),
            ('<SPAN CLASS="other star-pagination">*12</SPAN>', 1),
            ('<SPAN CLASS="ldml-pagenumber"/>', 1),
            ('<A CLASS="page-label">*12</A>', 1),
            ('<p class="star-pagination page-label">Fictional paragraph</p>', 0),
            ('<span class="star-pagination-extra">Fictional label</span>', 0),
            ('<a class="other">Fictional link</a>', 0),
        )
        for original, expected in fixtures:
            with self.subTest(original=original):
                self.assertEqual(page_locator_count("html", original), expected)

    def test_nav_raw_text_and_nested_locators(self) -> None:
        fixtures = (
            ('<nav><page-number>*12</page-number></nav>', 1),
            ('<style><page-number>*12</page-number></style>', 0),
            ('<script><page-number>*12</page-number></script>', 0),
            ('<style><page-number>*12</page-number></style><page-number/>', 1),
            ('<page-number><span class="star-pagination">*12</span></page-number>', 2),
        )
        for original, expected in fixtures:
            with self.subTest(original=original):
                self.assertEqual(page_locator_count("xml_harvard", original), expected)

    def test_plain_text_is_zero_without_parsing(self) -> None:
        with patch.object(HTMLParser, "feed", side_effect=AssertionError("plain text must not be parsed")):
            self.assertEqual(page_locator_count("plain_text", TEXT + "<page-number/>"), 0)


class Overlap(unittest.TestCase):
    """Intersection lengths count code points, including a planted mislabel."""

    def test_hand_computed_matrix_and_figures(self) -> None:
        candidate = (
            LabelSpan(0, 10, "majority"),
            LabelSpan(10, 15, "dissent"),
            LabelSpan(15, 20, "unknown"),
        )
        checked = (
            LabelSpan(0, 8, "majority"),
            LabelSpan(8, 15, "dissent"),
            LabelSpan(15, 20, "unknown"),
        )
        # [0, 8) agrees; [8, 10) is dissent under majority; the last
        # two candidate sections agree for five characters each.
        matrix = overlap_matrix(candidate, checked)
        self.assertEqual(matrix, {
            "majority": {"majority": 8, "dissent": 2},
            "dissent": {"dissent": 5},
            "unknown": {"unknown": 5},
        })
        self.assertEqual(case_figures(matrix), CaseFigures(20, 2, 5, matrix))

    def test_planted_dissent_paragraph_is_zero_tolerance_only(self) -> None:
        candidate = (
            LabelSpan(0, 21, "majority"), LabelSpan(21, 22, "dissent"),
        )
        checked = (
            LabelSpan(0, 20, "majority"), LabelSpan(20, 22, "dissent"),
        )
        # One checked dissent character sits under majority; 20/21 remains
        # above the precision floor, and there are no unknown characters.
        matrix = overlap_matrix(candidate, checked)
        self.assertEqual(case_figures(matrix).mislabel, 1)
        self.assertEqual(
            slice_gate(matrix, counted=1, measured=1, differing=0).reasons,
            ("separate-opinion text under a court-opinion type",),
        )

    def test_protected_type_sets_follow_the_decision(self) -> None:
        self.assertEqual(COURT_TYPES, frozenset({"majority", "plurality", "per_curiam"}))
        self.assertEqual(SEPARATE_TYPES, frozenset({"concurrence", "dissent", "concurrence_dissent"}))
        self.assertTrue(set(SECTION_TYPES) >= COURT_TYPES | SEPARATE_TYPES)
        for candidate in COURT_TYPES:
            for checked in SEPARATE_TYPES:
                with self.subTest(candidate=candidate, checked=checked):
                    self.assertEqual(case_figures({candidate: {checked: 1}}).mislabel, 1)


class Agreement(unittest.TestCase):
    """Recorded section boundaries and types must match the candidate."""

    def test_equal_and_differing_sections(self) -> None:
        candidate = (LabelSpan(0, 4, "majority"), LabelSpan(4, 7, "dissent"))
        recorded = (StoredSection(0, 4, "majority", "line"), StoredSection(4, 7, "dissent", "row"))
        self.assertTrue(record_agrees(candidate, recorded))
        self.assertFalse(record_agrees(candidate, tuple(reversed(recorded))))
        self.assertFalse(record_agrees(candidate, recorded[:1]))
        self.assertFalse(record_agrees(candidate, (recorded[0], StoredSection(5, 7, "dissent", "row"))))
        self.assertFalse(record_agrees(candidate, (recorded[0], StoredSection(4, 7, "concurrence", "row"))))


class Gate(unittest.TestCase):
    """Every bound holds at its edge; absent types have no precision."""

    def test_page_locator_equality_is_zero_tolerance(self) -> None:
        matrix = {"majority": {"majority": len(TEXT)}}
        for differing in (0, 1):
            with self.subTest(differing=differing):
                verdict = slice_gate(matrix, counted=1, measured=1, differing=differing)
                self.assertEqual(verdict.ok, differing == 0)
                self.assertEqual(verdict.reasons, () if differing == 0 else ("page locators and markers differ",))

    def test_precision_and_unknown_at_their_edges(self) -> None:
        self.assertEqual((PRECISION_FLOOR, UNKNOWN_CEILING), (0.95, 0.15))
        # Majority precision is 19/20; unknown share is 6/40.
        matrix = {
            "majority": {"majority": 19, "headmatter": 1},
            "headmatter": {"headmatter": 14},
            "unknown": {"unknown": 6},
        }
        verdict = slice_gate(matrix, counted=1, measured=1, differing=0)
        self.assertTrue(verdict.ok, verdict.reasons)
        self.assertEqual(verdict.reasons, ())

        below_precision = {**matrix, "majority": {"majority": 18, "headmatter": 2}}
        self.assertIn("majority precision below floor", slice_gate(below_precision, 1, 1, 0).reasons)
        above_unknown = {**matrix, "headmatter": {"headmatter": 13}, "unknown": {"unknown": 7}}
        self.assertEqual(slice_gate(above_unknown, 1, 1, 0).reasons, ("unknown share above ceiling",))

    def test_unknown_has_no_precision_figure(self) -> None:
        # Unknown abstains: 6 characters checked as majority leave its 6/100
        # share under the ceiling and no precision reason.
        matrix = {"majority": {"majority": 94}, "unknown": {"majority": 6}}
        self.assertEqual(slice_gate(matrix, 1, 1, 0).reasons, ())

    def test_absent_types_have_no_figure_and_cases_must_be_measured(self) -> None:
        matrix = {"majority": {"majority": 10}}
        self.assertTrue(slice_gate(matrix, 1, 1, 0).ok)
        self.assertEqual(slice_gate(matrix, 0, 0, 0).reasons, ("no labelled case",))
        self.assertEqual(
            slice_gate(matrix, 2, 1, 0).reasons,
            ("not every counted case was measured",),
        )


class Runner(unittest.TestCase):
    """The candidate, record, and checked labels jointly decide each case."""

    def test_clean_case_and_content_free_report(self) -> None:
        host = SegmenterHost()
        result = run_segmenter(_loaded(), "segmenter-300", _context(host))
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(result.results[0].verdict, "pass")
        self.assertEqual(result.results[0].metrics["mislabel"], 0)
        self.assertTrue(result.results[0].metrics["record_agrees"])
        self.assertEqual(set(result.results[0].metrics), {
            "chars", "mislabel", "unknown", "matrix", "text_source", "record_agrees",
            "page_elements", "page_markers", "pgmap_checked", "pgmap_disagreeing",
        })
        for key in ("page_elements", "page_markers", "pgmap_checked", "pgmap_disagreeing"):
            self.assertIs(type(result.results[0].metrics[key]), int)
            self.assertEqual(result.results[0].metrics[key], 0)
        self.assertIsNone(result.results[0].judge)
        self.assertIsNotNone(result.results[0].latency_ms)
        self.assertNotIn(TEXT, result.report)
        self.assertNotIn("precision", next(line for line in result.report.splitlines() if line.startswith("type unknown:")) if "type unknown:" in result.report else "")
        argv, sql = next((argv, sql) for argv, sql in host.calls if sql and "\\set opinion_ids " in sql)
        from gideon.evaluation import record

        self.assertEqual(list(argv), record.reader_argv("/tmp/segmenter-rendered"))
        assert sql is not None
        self.assertEqual(sql.count("\\set opinion_ids "), 1)
        self.assertIn("jsonb_array_elements_text(:'opinion_ids'::jsonb)", sql)
        self.assertIn("d.status = 'ready'", sql)
        self.assertIn("s.char_start", sql)
        self.assertIn("s.section_type", sql)
        self.assertNotIn(TEXT, sql)
        assert host.leg_input is not None
        self.assertEqual(json.loads(host.leg_input)["per_curiam"], False)

    def test_matching_page_marker_passes_with_all_four_counts(self) -> None:
        host, loaded = SegmenterHost(), _loaded()
        case_id = "segmenter-001"
        original = f"<root><page-number>Fictional page label</page-number><p>{TEXT}</p></root>"
        _markup_fixture(host, loaded, case_id, original)
        host.leg_markers[case_id] = [
            ["page", "Fictional page label", 0],
            ["editorial", "Fictional editorial label", 0],
        ]
        host.pgmap_counts[case_id] = (2, 1)
        result = run_segmenter(loaded, "segmenter-300", _context(host))
        self.assertTrue(result.verdict, result.report)
        self.assertEqual(result.results[0].verdict, "pass")
        metrics = result.results[0].metrics
        self.assertEqual(
            tuple(metrics[key] for key in ("page_elements", "page_markers", "pgmap_checked", "pgmap_disagreeing")),
            (1, 1, *host.pgmap_counts[case_id]),
        )
        self.assertNotIn("problem", metrics)
        self.assertIn(
            "page xml_harvard: 1 locators, 1 markers, differing ids none; pgmap checked 2, disagreeing 1",
            result.report.splitlines(),
        )
        for sentinel in (TEXT, original, "Fictional page label", "Fictional editorial label"):
            self.assertNotIn(sentinel, result.report + repr(result.results))

    def test_dropped_page_locator_fails_even_when_the_record_is_stale(self) -> None:
        for stale in (False, True):
            with self.subTest(stale=stale):
                host, loaded = SegmenterHost(), _loaded()
                # The canonical TEXT is unchanged when a locator in nav is dropped.
                original = f"<root><nav><page-number>Fictional page label</page-number></nav><p>{TEXT}</p></root>"
                _markup_fixture(host, loaded, "segmenter-001", original)
                if stale:
                    host.documents[0]["sections"] = [[0, len(TEXT), "majority", "row"]]
                result = run_segmenter(loaded, "segmenter-300", _context(host))
                self.assertFalse(result.verdict)
                self.assertEqual(result.results[0].verdict, "fail")
                metrics = result.results[0].metrics
                self.assertEqual(metrics["problem"], "locator-count-differs")
                self.assertEqual((metrics["page_elements"], metrics["page_markers"]), (1, 0))
                self.assertEqual(metrics["record_agrees"], not stale)
                self.assertEqual(metrics["mislabel"], 0)
                lines = result.report.splitlines()
                self.assertIn(
                    "page xml_harvard: 1 locators, 0 markers, differing ids segmenter-001; pgmap checked 0, disagreeing 0",
                    lines,
                )
                self.assertIn("gate: page locators and markers differ", lines)
                self.assertIn("locator-count-differs 1 ids segmenter-001", result.report)
                self.assertIn(
                    "Compare the named case's page locators with the walk's markers; "
                    "a locator the walk drops is a finding on the opinion parser, never a moved bound.",
                    result.report,
                )
                self.assertIn("Fix:", result.report)
                self.assertNotIn("record-stale", result.report)
                for sentinel in (TEXT, original, "Fictional page label"):
                    self.assertNotIn(sentinel, result.report + repr(result.results))

    def test_pgmap_counts_sum_per_source_without_gating(self) -> None:
        for sources in (("xml_harvard", "xml_harvard"), ("html", "xml_harvard")):
            with self.subTest(sources=sources):
                host, loaded = SegmenterHost(), _loaded(second=True)
                host.documents.append(_record_document(102))
                ids = loaded.active_ids
                host.pgmap_counts = {ids[0]: (2, 1), ids[1]: (3, 2)}
                for index, (case_id, source) in enumerate(zip(ids, sources, strict=True)):
                    original = f"<root><p data-fixture='{index}'>{TEXT}</p></root>"
                    _markup_fixture(host, loaded, case_id, original, column=source)
                result = run_segmenter(loaded, "segmenter-300", _context(host))
                self.assertTrue(result.verdict, result.report)
                expected_lines: list[str] = []
                for source in SEGMENTER_TEXT_SOURCES:
                    counts = [host.pgmap_counts[case_id] for case_id, column in zip(ids, sources, strict=True) if column == source]
                    if counts:
                        checked = sum(item[0] for item in counts)
                        disagreeing = sum(item[1] for item in counts)
                        expected_lines.append(
                            f"page {source}: 0 locators, 0 markers, differing ids none; "
                            f"pgmap checked {checked}, disagreeing {disagreeing}"
                        )
                lines = result.report.splitlines()
                self.assertEqual([line for line in lines if line.startswith("page ")], expected_lines)
                self.assertFalse(any(line.startswith("gate:") for line in lines))
                self.assertLess(
                    max(index for index, line in enumerate(lines) if line.startswith("unknown ")),
                    min(index for index, line in enumerate(lines) if line.startswith("page ")),
                )
                for item in result.results:
                    self.assertEqual(
                        (item.metrics["pgmap_checked"], item.metrics["pgmap_disagreeing"]),
                        host.pgmap_counts[item.case_id],
                    )
                    self.assertEqual(item.verdict, "pass")
                self.assertNotIn(TEXT, result.report + repr(result.results))

    def test_problem_vocabulary_is_closed(self) -> None:
        self.assertEqual(PROBLEMS, frozenset({
            "record-unreachable", "not-ingested", "original-missing", "text-moved",
            "record-stale", "image-leg-failed", "locator-count-differs",
        }))

    def test_all_individual_failure_codes_and_fixes(self) -> None:
        scenarios = (
            ("not-ingested", lambda host: host.documents.clear(), _loaded()),
            ("original-missing", lambda host: host.originals.clear(), _loaded()),
            ("original-missing", lambda host: host.originals.update({TEXT_HASH: b"corrupt"}), _loaded()),
            ("text-moved", lambda host: None, _loaded(label_hash="a" * 64)),
            ("text-moved", lambda host: host.documents[0].update(canonical_sha256="a" * 64), _loaded()),
            ("record-stale", lambda host: host.documents[0].update(sections=[[0, len(TEXT), "majority", "row"]]), _loaded()),
        )
        for code, change, loaded in scenarios:
            with self.subTest(code=code):
                host = SegmenterHost()
                change(host)
                result = run_segmenter(loaded, "segmenter-300", _context(host))
                self.assertFalse(result.verdict)
                self.assertEqual(result.results[0].metrics["problem"], code)
                self.assertIn(code, result.report)
                self.assertIn("Fix:", result.report)
                self.assertNotIn(TEXT, result.report)
                if code == "record-stale":
                    self.assertEqual(result.results[0].metrics["mislabel"], 0)
                    self.assertFalse(result.results[0].metrics["record_agrees"])

    def test_failed_read_or_leg_fails_every_counted_case(self) -> None:
        for failure in ("read-exit", "read-json", "leg-exit", "leg-json"):
            with self.subTest(failure=failure):
                host = SegmenterHost()
                host.documents.append(_record_document(102))
                if failure == "read-exit":
                    host.read_rc = 2
                elif failure == "read-json":
                    host.read_stdout = "not-json"
                elif failure == "leg-exit":
                    host.leg_rc = 2
                else:
                    host.leg_stdout = "not-json\n"
                result = run_segmenter(_loaded(second=True), "segmenter-300", _context(host))
                expected = "record-unreachable" if failure.startswith("read") else "image-leg-failed"
                self.assertFalse(result.verdict)
                self.assertEqual(tuple(item.metrics["problem"] for item in result.results), (expected, expected))
                self.assertIn("Fix:", result.report)
                if failure == "leg-exit":
                    self.assertIn("segmenter image leg failed: container exited 2\n", result.report)
                if failure.startswith("read"):
                    self.assertFalse(any(argv[:2] == ("docker", "run") for argv, _ in host.calls))

    def test_no_image_access_never_calls_leg(self) -> None:
        host = SegmenterHost()
        result = run_segmenter(_loaded(), "segmenter-300", _context(host, image=False))
        self.assertEqual(result.results[0].metrics["problem"], "image-leg-failed")
        self.assertEqual(host.calls, [])

    def test_flag_from_record_and_planted_mislabel_by_id(self) -> None:
        host = SegmenterHost()
        host.leg_sections = [
            [0, len(TEXT) - 6, "majority", "flag"],
            [len(TEXT) - 6, len(TEXT), "dissent", "row"],
        ]
        host.documents[0]["sections"] = host.leg_sections
        result = run_segmenter(_loaded(), "segmenter-300", _context(host))
        self.assertFalse(result.verdict)
        self.assertEqual(result.results[0].metrics["mislabel"], 1)
        self.assertEqual(result.results[0].verdict, "fail")
        self.assertIn("segmenter-001", result.report)
        assert host.leg_input is not None
        self.assertTrue(json.loads(host.leg_input)["per_curiam"])

    def test_report_gives_unknown_share_without_unknown_precision(self) -> None:
        host = SegmenterHost()
        host.leg_sections = [
            [0, len(TEXT) - 9, "majority", "row"],
            [len(TEXT) - 9, len(TEXT) - 7, "unknown", "none"],
            [len(TEXT) - 7, len(TEXT), "dissent", "row"],
        ]
        host.documents[0]["sections"] = host.leg_sections
        result = run_segmenter(_loaded(), "segmenter-300", _context(host))
        self.assertTrue(result.verdict, result.report)
        unknown_line = next(line for line in result.report.splitlines() if line.startswith("type unknown:"))
        self.assertEqual(unknown_line, "type unknown: 2 characters (abstained)")
        self.assertNotIn("precision", unknown_line)


class Command(unittest.TestCase):
    """The command counts only signed cases and never records a supplied set."""

    def test_temporary_set_clean_mislabel_stale_and_ci(self) -> None:
        from gideon.evaluation import segmenter_labels

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            cases = root / "build-gates" / "segmenter-300.jsonl"
            cases.parent.mkdir(parents=True)
            cases.write_text(
                "".join(json.dumps(case) + "\n" for case in (_case("segmenter-001", 101), _case("segmenter-002", 102))),
                encoding="utf-8",
            )
            label_path = root / segmenter_labels.LABELS_PATH
            label_path.write_text(segmenter_labels.serialize(_label()), encoding="utf-8")
            id_list = root / "slices" / "segmenter-300" / "sample.ids"
            id_list.parent.mkdir(parents=True)
            id_list.write_text("segmenter-001\nsegmenter-002\n", encoding="utf-8")

            for state in ("clean", "mislabel", "stale"):
                with self.subTest(state=state):
                    host = SegmenterHost()
                    if state == "mislabel":
                        host.leg_sections = [
                            [0, len(TEXT) - 6, "majority", "row"],
                            [len(TEXT) - 6, len(TEXT), "dissent", "row"],
                        ]
                        host.documents[0]["sections"] = host.leg_sections
                    elif state == "stale":
                        host.documents[0]["sections"] = [[0, len(TEXT), "majority", "row"]]
                    code, stdout, stderr = _invoke(
                        ["eval", "run", "--slice", "segmenter-300", "--set", str(root)],
                        **_run_kwargs(host),
                    )
                    self.assertEqual(stderr, "")
                    self.assertEqual(code, 0 if state == "clean" else 1, stdout)
                    self.assertIn("1 active cases evaluated, 1 unsigned excluded", stdout)
                    self.assertIn("record: ok — skipped", stdout)
                    self.assertFalse(any(sql and "INSERT INTO eval_runs" in sql for _, sql in host.calls))
                    self.assertNotIn(TEXT, stdout)
                    if state != "clean":
                        self.assertIn("segmenter-001", stdout)
            host = SegmenterHost()
            with patch("gideon.evaluation.command.stacks.secrets.select_directory"):
                code, stdout, stderr = _invoke(
                    ["eval", "run", "--slice", "segmenter-300", "--stack", "ci", "--set", str(root)],
                    **_run_kwargs(host),
                )
            self.assertEqual((code, stderr), (0, ""), stdout)
            from gideon.evaluation import stacks

            read_argv = next(argv for argv, sql in host.calls if sql and "\\set opinion_ids " in sql)
            self.assertIn(str(stacks.CI_ROOT), read_argv)
