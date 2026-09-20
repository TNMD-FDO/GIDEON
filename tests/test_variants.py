"""Fictional unit and command cases for extraction variant derivation."""

import ast
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from tools.exportboundary import absent_from_export
from tools.variants import VariantRefused, axes, derive
from tools.variants.__main__ import main

ROOT = Path(__file__).resolve().parents[1]


def _label(question: str, object_type: str, key: str | None = None) -> dict[str, object]:
    prefix = "[FICTIONAL TEST ONLY] "
    start = len(prefix) if question.startswith(prefix) else 0
    text = question[start:].rstrip(".")
    value: dict[str, object] = {
        "type": object_type,
        "start": start,
        "end": start + len(text),
        "text": text,
        "subsections": [],
    }
    if key is not None:
        value["key"] = key
    return value


def _case(question: str, labels: list[dict[str, object]], identifier: str = "extraction-001") -> dict[str, object]:
    return {
        "id": identifier,
        "question": question,
        "expected": {"objects": labels},
        "labels": ["invented"],
        "suite": "build-gates",
        "category": "extraction",
        "branch": "legal",
        "cluster_id": "fictional-cluster",
    }


class Axes(TestCase):
    """Each current axis has a fictional application and decline."""

    def test_code_axes(self) -> None:
        dotted = "[FICTIONAL TEST ONLY] 18 USC § 981A(a)."
        parent = _case(dotted, [_label(dotted, "statute", "/us/usc/t18/s981A")])
        self.assertIn("18 U.S.C.", derive(parent, axes.CURRENT["code-dotted"])[0])  # type: ignore[index]
        bare = "[FICTIONAL TEST ONLY] 18 U.S.C. § 981A(a)."
        parent["question"] = bare
        parent["expected"]["objects"] = [_label(bare, "statute", "/us/usc/t18/s981A")]  # type: ignore[index]
        self.assertIn("18 USC", derive(parent, axes.CURRENT["code-bare"])[0])  # type: ignore[index]
        self.assertIn("U.S. Code", derive(parent, axes.CURRENT["code-word"])[0])  # type: ignore[index]
        self.assertIsNone(derive(parent, axes.CURRENT["code-dotted"]))

    def test_sign_axes(self) -> None:
        question = "[FICTIONAL TEST ONLY] 18 U.S.C. 981(g)."
        parent = _case(question, [_label(question, "statute", "/us/usc/t18/s981")])
        self.assertIn("§ 981", derive(parent, axes.CURRENT["sign-present"])[0])  # type: ignore[index]
        marked = question.replace(" 981", " § 981")
        parent = _case(marked, [_label(marked, "statute", "/us/usc/t18/s981")])
        self.assertNotIn("§", derive(parent, axes.CURRENT["sign-absent"])[0])  # type: ignore[index]
        dotted = "[FICTIONAL TEST ONLY] § 981.1"
        self.assertIsNone(
            derive(_case(dotted, [_label(dotted, "bare_section")]), axes.CURRENT["sign-absent"])
        )

    def test_spacing_axes(self) -> None:
        question = "[FICTIONAL TEST ONLY] § 981(g)."
        parent = _case(question, [_label(question, "bare_section")])
        self.assertIn("§981", derive(parent, axes.CURRENT["spacing-tight"])[0])  # type: ignore[index]
        self.assertIn("\u00a0", derive(parent, axes.CURRENT["spacing-nbsp"])[0])  # type: ignore[index]

    def test_form_axis_covers_federal_habeas_and_supreme_rules(self) -> None:
        cases = (
            (
                "[FICTIONAL TEST ONLY] Fed. R. Crim. P. 41(b).",
                "court_rule",
                "/us/usc/t18a/courtRules/Crim/rule41",
            ),
            (
                "[FICTIONAL TEST ONLY] Rule 6 of the Rules Governing Section 2254 Cases.",
                "habeas_rule",
                "rules/2254/rule6",
            ),
            (
                "[FICTIONAL TEST ONLY] Sup. Ct. R. 14.1(a).",
                "scotus_rule",
                "rules/scotus/rule14",
            ),
        )
        for question, object_type, key in cases:
            with self.subTest(question):
                result = derive(
                    _case(question, [_label(question, object_type, key)]),
                    axes.CURRENT["form-long-short"],
                )
                self.assertIsNotNone(result)
                self.assertNotEqual(result[0], question)  # type: ignore[index]

    def test_docket_marker_and_lower_case_axes(self) -> None:
        question = "[FICTIONAL TEST ONLY] 3:21-CR-00123-ABCD-2."
        parent = _case(question, [_label(question, "docket")])
        result = derive(parent, axes.CURRENT["docket-marker"])
        self.assertEqual(result[0], "[FICTIONAL TEST ONLY] No. 3:21-CR-00123-ABCD-2.")  # type: ignore[index]
        two_part = "[FICTIONAL TEST ONLY] Case No. 21-5123."
        two_parent = _case(two_part, [_label(two_part[two_part.index("21-") : -1], "docket")])
        two_parent["expected"]["objects"][0]["start"] = two_part.index("21-")  # type: ignore[index]
        two_parent["expected"]["objects"][0]["end"] = len(two_part) - 1  # type: ignore[index]
        self.assertIn("Docket No.", derive(two_parent, axes.CURRENT["docket-marker"])[0])  # type: ignore[index]
        for marker, successor in zip(
            ("No.", "Case No.", "Docket No.", "Dkt."),
            ("Case No.", "Docket No.", "Dkt.", "No."),
            strict=True,
        ):
            marked = f"[FICTIONAL TEST ONLY] {marker} 21-5123"
            marker_start = marked.index("21-")
            marked_label = _label(marked[marker_start:], "docket")
            marked_label["start"], marked_label["end"] = marker_start, len(marked)
            marked_result = derive(
                _case(marked, [marked_label]), axes.CURRENT["docket-marker"]
            )
            self.assertIn(successor, marked_result[0])  # type: ignore[index]
        self.assertIsNone(derive(_case("[FICTIONAL TEST ONLY] 21-5123", [_label("21-5123", "docket")]), axes.CURRENT["docket-marker"]))

        lower = "[FICTIONAL TEST ONLY] 18 U.S.C. § 981A(a)(A)."
        result = derive(_case(lower, [_label(lower, "statute", "/us/usc/t18/s981A")]), axes.CURRENT["lower-case"])
        self.assertIn("981A(a)(A)", result[0])  # type: ignore[index]
        guideline = "[FICTIONAL TEST ONLY] Guidelines § 2B1.1(A)."
        result = derive(_case(guideline, [_label(guideline, "guideline", "ussg/2B1.1")]), axes.CURRENT["lower-case"])
        self.assertIn("2B1.1(A)", result[0])  # type: ignore[index]

    def test_core_and_overlap_refusals_name_only_id_and_axis(self) -> None:
        question = "[FICTIONAL TEST ONLY] 18 U.S.C. § 981A."
        label = _label(question, "statute", "/us/usc/t18/s981A")

        def breaks_core(text: str, _: object) -> axes.Edit:
            return axes.Edit(0, len(text), text.replace("981A", "981a"), 0, len(text))

        axis = axes.Axis("fictional-core", 1, breaks_core)
        with self.assertRaisesRegex(VariantRefused, r"^extraction-001 fictional-core@1$"):
            derive(_case(question, [label]), axis)

        def overlaps(text: str, _: object) -> axes.Edit:
            return axes.Edit(0, len(text), text, 0, len(text))

        first = _label("[FICTIONAL TEST ONLY] A", "caption")
        first["start"], first["end"] = 0, 25
        second = _label("18 U.S.C. § 981A", "statute", "/us/usc/t18/s981A")
        second["start"], second["end"] = 20, 36
        with self.assertRaisesRegex(VariantRefused, r"^extraction-001 fictional-overlap@1$"):
            derive(_case(question, [first, second]), axes.Axis("fictional-overlap", 1, overlaps))

    def test_offsets_shift_around_an_unlanded_object(self) -> None:
        question = "[FICTIONAL TEST ONLY] 18 U.S.C. § 981(a), caption, 21 CFR 2.20."
        first_start = question.index("18 U.S.C.")
        second_start = question.index("21 CFR")
        first = _label(question[first_start : question.index(",")], "statute", "/us/usc/t18/s981")
        first["start"], first["end"] = first_start, question.index(",")
        middle_start = question.index("caption")
        middle = {"type": "caption", "start": middle_start, "end": middle_start + 7, "text": "caption", "subsections": []}
        second = _label(question[second_start:], "regulation", "cfr/21/2.20")
        second["start"], second["end"] = second_start, len(question)
        def prepend(text: str, label: object) -> axes.Edit:
            start = label["start"]  # type: ignore[index]
            end = label["end"]  # type: ignore[index]
            return axes.Edit(start, end, "X " + text[start:end], 2, len(text[start:end]) + 2)

        result = derive(
            _case(question, [first, middle, second]),
            axes.Axis("fictional-offset", 1, prepend),
        )
        self.assertEqual(result[1][1]["text"], "caption")  # type: ignore[index]
        self.assertEqual(result[1][2]["start"], second_start + 4)  # type: ignore[index]


class Command(TestCase):
    """The command checks, sheets, writes, and refuses fictional files."""

    def test_committed_variants_rederive_line_by_line(self) -> None:
        pairs = (
            (
                ROOT / "eval/sets/eval-v1/build-gates/extraction-invented.jsonl",
                ROOT / "eval/sets/eval-v1/build-gates/extraction-invented-variants.jsonl",
            ),
            (
                ROOT / "eval/sets/eval-v1/build-gates/extraction.jsonl",
                ROOT / "eval/sets/eval-v1/build-gates/extraction-variants.jsonl",
            ),
        )
        axes_by_id = {axis.axis_id: axis for axis in axes.AXES}
        for parent_path, variant_path in pairs:
            if absent_from_export(variant_path.relative_to(ROOT), ROOT):
                continue
            parents = {
                case["id"]: case
                for case in (json.loads(line) for line in parent_path.read_text().splitlines())
            }
            for line in variant_path.read_text().splitlines():
                variant = json.loads(line)
                axis = axes_by_id[variant["labels"][1]]
                result = derive(parents[variant["parent"]], axis)
                if result is None:
                    self.fail(variant["id"])
                self.assertEqual(result[0], variant["question"], variant["id"])
                self.assertEqual(result[1], variant["expected"]["objects"], variant["id"])

    def test_old_axis_rederives_and_current_axis_writes_superseding_variant(self) -> None:
        question = "[FICTIONAL TEST ONLY] 18 U.S.C. § 981."
        label = _label(question, "statute", "/us/usc/t18/s981")

        def old_edit(text: str, _: object) -> axes.Edit:
            return axes.Edit(0, len(text), "OLD " + text, 4, len(text) + 4)

        def new_edit(text: str, _: object) -> axes.Edit:
            return axes.Edit(0, len(text), "NEW " + text, 4, len(text) + 4)

        old = axes.Axis("fictional", 1, old_edit)
        new = axes.Axis("fictional", 2, new_edit)
        parent = _case(question, [label], "extraction-001")
        old_question, old_objects = derive(parent, old)  # type: ignore[misc]
        old_variant = _case(old_question, old_objects, "extraction-002")
        old_variant["labels"] = ["variant", old.axis_id]
        old_variant["parent"] = parent["id"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents = root / "parents.jsonl"
            variants = root / "variants.jsonl"
            parents.write_text(json.dumps(parent) + "\n", encoding="utf-8")
            variants.write_text(json.dumps(old_variant) + "\n", encoding="utf-8")
            with patch.object(axes, "AXES", (old, new)), patch.object(axes, "CURRENT", {"fictional": new}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main([str(parents), str(variants)]), 0)
                self.assertIn("extraction-003", output.getvalue())
                self.assertEqual(main([str(parents), str(variants), "--write", "--reviewer", "FICTIONAL", "--on", "2026-09-19"]), 0)
            written = [json.loads(line) for line in variants.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(written[-1]["supersedes"], "extraction-002")
        self.assertEqual(written[-1]["labels"], ["variant", "fictional@2"])

    def test_the_manifest_allocates_ids_over_files_the_command_never_names(self) -> None:
        question = "[FICTIONAL TEST ONLY] 18 U.S.C. § 981."
        parent = _case(question, [_label(question, "statute", "/us/usc/t18/s981")], "extraction-001")
        elsewhere = _case(question, [], "extraction-009")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents = root / "parents.jsonl"
            variants = root / "variants.jsonl"
            other = root / "other.jsonl"
            parents.write_text(json.dumps(parent) + "\n", encoding="utf-8")
            variants.write_text("", encoding="utf-8")
            other.write_text(json.dumps(elsewhere) + "\n", encoding="utf-8")
            (root / "series.txt").write_text("parents.jsonl\nvariants.jsonl\nother.jsonl\n")
            self.assertEqual(
                main([str(parents), str(variants), "--write", "--reviewer", "FICTIONAL", "--on", "2026-09-19"]),
                0,
            )
            written = [json.loads(line) for line in variants.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(written)
        self.assertEqual(written[0]["id"], "extraction-010")

    def test_write_refuses_when_the_manifest_names_an_absent_file(self) -> None:
        question = "[FICTIONAL TEST ONLY] 18 U.S.C. § 981."
        parent = _case(question, [_label(question, "statute", "/us/usc/t18/s981")], "extraction-001")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents = root / "parents.jsonl"
            variants = root / "variants.jsonl"
            parents.write_text(json.dumps(parent) + "\n", encoding="utf-8")
            variants.write_text("", encoding="utf-8")
            (root / "series.txt").write_text("parents.jsonl\nvariants.jsonl\nexcluded.jsonl\n")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [str(parents), str(variants), "--write", "--reviewer", "FICTIONAL", "--on", "2026-09-19"]
                )
            self.assertEqual(code, 1)
            self.assertIn("excluded.jsonl", output.getvalue())
            self.assertIn("development tree", output.getvalue())
            self.assertEqual(variants.read_text(encoding="utf-8"), "")

    def test_write_refuses_a_series_with_a_gap(self) -> None:
        question = "[FICTIONAL TEST ONLY] 18 U.S.C. § 981."
        parent = _case(question, [_label(question, "statute", "/us/usc/t18/s981")], "extraction-002")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents = root / "parents.jsonl"
            variants = root / "variants.jsonl"
            parents.write_text(json.dumps(parent) + "\n", encoding="utf-8")
            variants.write_text("", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [str(parents), str(variants), "--write", "--reviewer", "FICTIONAL", "--on", "2026-09-19"]
                )
            self.assertEqual(code, 1)
            self.assertIn("extraction-001", output.getvalue())
            self.assertEqual(variants.read_text(encoding="utf-8"), "")

    def test_a_refusal_while_listing_missing_pairs_names_the_id_and_fails(self) -> None:
        question = "[FICTIONAL TEST ONLY] 18 U.S.C. § 981."
        parent = _case(question, [_label(question, "statute", "/us/usc/t18/s981")], "extraction-001")

        def wrecking_edit(text: str, _: object) -> axes.Edit:
            return axes.Edit(0, len(text), "no object here", 0, 14)

        axis = axes.Axis("fictional", 1, wrecking_edit)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents = root / "parents.jsonl"
            variants = root / "variants.jsonl"
            parents.write_text(json.dumps(parent) + "\n", encoding="utf-8")
            variants.write_text("", encoding="utf-8")
            with patch.object(axes, "AXES", (axis,)), patch.object(axes, "CURRENT", {"fictional": axis}):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main([str(parents), str(variants)])
        self.assertEqual(code, 1)
        self.assertIn("refused extraction-001 fictional@1", output.getvalue())
        self.assertNotIn(question, output.getvalue())

    def test_sheet_shows_docket_replaced_range_and_check_refuses(self) -> None:
        question = "[FICTIONAL TEST ONLY] 3:21-cr-00123"
        start = question.index("3:")
        label = _label(question[start:], "docket")
        label["start"], label["end"] = start, len(question)
        parent = _case(question, [label])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parents = root / "parents.jsonl"
            variants = root / "variants.jsonl"
            parents.write_text(json.dumps(parent) + "\n", encoding="utf-8")
            variants.write_text("", encoding="utf-8")
            docket = axes.CURRENT["docket-marker"]
            with patch.object(axes, "AXES", (docket,)), patch.object(axes, "CURRENT", {"docket-marker": docket}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main([str(parents), str(variants), "--sheet"]), 0)
                self.assertIn("'3:21-cr-00123' -> 'No. 3:21-cr-00123'", output.getvalue())
                bad = _case("[FICTIONAL TEST ONLY] changed", [], "extraction-002")
                bad["labels"] = ["variant", docket.axis_id]
                bad["parent"] = parent["id"]
                variants.write_text(json.dumps(bad) + "\n", encoding="utf-8")
                self.assertEqual(main([str(parents), str(variants)]), 1)

    def test_variant_modules_import_only_standard_library_and_tools(self) -> None:
        for path in (Path("tools/variants/__init__.py"), Path("tools/variants/axes.py"), Path("tools/variants/__main__.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                self.assertFalse(any(name.startswith("gideon") for name in names), path)
