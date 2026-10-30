"""The segmenter workbook's layout, checked-mark intake, and byte-preserving rewrite."""

import ast
import io
import runpy
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_segmenter_kit import _intake_fixture, _run_intake

import tools.workbook
from gideon.evaluation import segmenter_labels
from gideon.host.render.worker import SECTION_TYPES
from tools.segmenter import workbook

SENTINEL = "FICTITIOUS_WORKBOOK_CELL_SENTINEL"


def _run(arguments: list[str]) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    code = workbook.main(arguments, stdout=output, stderr=errors)
    return code, output.getvalue(), errors.getvalue()


class RoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.set_root, self.sheets, self.manifest, self.text = _intake_fixture(self.root)
        self.path = self.root / "review.xlsx"
        for number in (1, 2):
            path = self.sheets / f"segmenter-{number:03d}.txt"
            extra = b"\nCited as: Fictitious v. Example\n"
            if number == 1:
                extra += b"Original: https://example.org/fictitious\n"
            path.write_bytes(path.read_bytes().replace(b"\nParagraphs:\n", extra + b"\nParagraphs:\n"))
            (self.sheets / f"segmenter-{number:03d}.full.txt").write_text(
                "0000\tFictitious majority\n\n0001\tFictitious dissent", encoding="utf-8",
            )

    def export(self) -> None:
        code, output, errors = _run(["export", str(self.sheets), str(self.path)])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, f"exported 2 cases to {self.path}\n")

    def load(self):
        book = tools.workbook.openpyxl.load_workbook(self.path)
        self.addCleanup(book.close)
        return book

    def checked(self) -> None:
        book = self.load()
        book["Cases"]["H2"] = "checked"
        book["Cases"]["H3"] = "checked"
        book.save(self.path)

    def snapshot(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.sheets.iterdir()}

    def import_(self, *, dry_run: bool = False) -> tuple[int, str, str]:
        arguments = ["import", str(self.path), str(self.sheets)]
        if dry_run:
            arguments.append("--dry-run")
        return _run(arguments)

    def test_export_matches_hand_computed_cells_and_styles(self) -> None:
        (self.sheets / "segmenter-001.txt.save").write_text(SENTINEL, encoding="utf-8")
        self.export()
        book = self.load()
        self.assertEqual(book.sheetnames, ["Start here", "Cases", "segmenter-001", "segmenter-002"])
        self.assertEqual(tuple(book["Start here"].values), tuple((line or None,) for line in workbook.GUIDE))
        self.assertEqual([cell.row for row in book["Start here"] for cell in row if cell.font.bold], [1, 16, 24])
        index = book["Cases"]
        self.assertEqual(tuple(index.values), (
            ("Case", "Court", "Paragraphs", "Draft status", "Draft starts", "Draft unknown", "Original", "Verdict", "Notes"),
            ("segmenter-001", "ca6", 2, "drafted", 2, 0, "open", None, None),
            ("segmenter-002", "ca6", 2, "drafted", 2, 0, None, None, None),
        ))
        self.assertEqual([index.column_dimensions[letter].width for letter in "ABCDEFGHI"],
                         [16, 8, 12, 13, 13, 14, 12, 12, 60])
        self.assertEqual(index["G2"].hyperlink.target, "https://example.org/fictitious")
        self.assertEqual(index["G3"].data_type, "n")
        self.assertIsNone(index["G3"].hyperlink)
        self.assertEqual(index["A2"].hyperlink.target, "#'segmenter-001'!A1")
        verdicts = index.data_validations.dataValidation
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].formula1, '"checked,unsettled"')
        self.assertEqual(str(verdicts[0].sqref), "H2 H3")
        self.assertTrue(verdicts[0].allow_blank)
        self.assertFalse(verdicts[0].showErrorMessage)
        self.assertIsNone(verdicts[0].errorTitle)
        self.assertIsNone(verdicts[0].error)
        for number in (1, 2):
            case_id = f"segmenter-{number:03d}"
            tab = book[case_id]
            columns = 5 if number == 1 else 4
            expected = (
                ("← Cases", case_id, None, "court ca6 · 2 paragraphs · draft drafted", "Original (CourtListener)"),
                (None, None, None, "Cited as: Fictitious v. Example", None),
                (None, None, None, None, None),
                ("Para", "Mark", "Draft", "Text", None),
                (0, "majority", "majority", "Fictitious majority", None),
                (1, "dissent", "dissent", "Fictitious dissent", None),
            )
            self.assertEqual(tuple(tab.values), tuple(row[:columns] for row in expected))
            self.assertEqual([tab.column_dimensions[letter].width for letter in "ABCD"], [7, 21, 21, 110])
            self.assertEqual(tab.freeze_panes, "B5")
            self.assertEqual(tab["A5"].number_format, "0000")
            self.assertEqual(tab["C5"].font.color.rgb, "00808080")
            self.assertEqual(tab["A1"].hyperlink.target, "#'Cases'!A1")
            marks = tab.data_validations.dataValidation
            self.assertEqual(len(marks), 1)
            self.assertEqual(marks[0].formula1, '"' + ",".join(("-", *SECTION_TYPES)) + '"')
            self.assertEqual(str(marks[0].sqref), "B5 B6")
            self.assertFalse(marks[0].allow_blank)
            self.assertTrue(marks[0].showErrorMessage)
            self.assertEqual(marks[0].errorTitle, "Not a mark")
            self.assertEqual(marks[0].error, "Pick a type from the list, or - to continue the section above.")
            self.assertEqual([rule.formula for span in tab.conditional_formatting
                              for rule in tab.conditional_formatting[span]], [
                ['$B5="unknown"'], ['AND($B5<>"",$B5<>"-")'], ['AND($A5<>"",$B5<>$C5)'],
            ])

    def test_untouched_book_and_checked_book_leave_sheet_bytes_unchanged(self) -> None:
        self.export()
        before = self.snapshot()
        code, output, errors = self.import_()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, "checked 0, unsettled 0, not yet checked 2\nunsettled: none\n"
                         "not yet checked: segmenter-001, segmenter-002\nwritten\n")
        self.assertEqual(self.snapshot(), before)
        self.checked()
        for _ in range(2):
            code, output, errors = self.import_()
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(output, "segmenter-001: 0 marks changed from the sheet\n"
                             "segmenter-002: 0 marks changed from the sheet\n"
                             "checked 2, unsettled 0, not yet checked 0\nunsettled: none\nwritten\n")
            self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "sheets-draft").exists())

    def test_rewrite_preserves_every_byte_outside_tokens_and_file_mode(self) -> None:
        for ending, final in ((b"\n", True), (b"\r\n", True), (b"\r", False), (b"\r\n", False)):
            with self.subTest(ending=ending, final=final):
                path = self.sheets / "segmenter-001.txt"
                payload = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                payload = payload.rstrip(b"\n").replace(b"\n", ending)
                if final:
                    payload += ending
                path.write_bytes(payload)
                path.chmod(0o640)
                self.path.unlink(missing_ok=True)
                self.export()
                self.checked()
                book = self.load()
                book["segmenter-001"]["B6"] = " [CONCURRENCE] "
                book.save(self.path)
                before = self.snapshot()
                code, output, errors = self.import_()
                self.assertEqual((code, errors), (0, ""))
                self.assertIn("segmenter-001: 1 marks changed", output)
                expected = dict(before)
                expected[path.name] = payload.replace(b"[dissent]", b"[concurrence]")
                self.assertEqual(self.snapshot(), expected)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
                # Restore the mark before the next independent newline form.
                path.write_bytes(payload)

    def test_dry_run_summary_and_rewritten_sheets_pass_intake(self) -> None:
        self.export()
        self.checked()
        book = self.load()
        book["segmenter-001"]["B6"] = " [] "
        book["Cases"]["I2"] = SENTINEL
        book.save(self.path)
        before = self.snapshot()
        dry_code, dry_output, dry_errors = self.import_(dry_run=True)
        self.assertEqual((dry_code, dry_errors), (0, ""))
        self.assertEqual(self.snapshot(), before)
        code, output, errors = self.import_()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(dry_output.replace("dry run: nothing written", "written"), output)
        self.assertNotIn(SENTINEL, output + errors)
        self.assertEqual((self.sheets / "segmenter-001.txt").read_bytes(),
                         before["segmenter-001.txt"].replace(b"[dissent]", b"-"))
        code, output, errors = _run_intake(self.manifest, self.sheets, set_root=self.set_root)
        self.assertEqual((code, errors), (0, ""), output)
        parsed = segmenter_labels.parse_bytes(segmenter_labels.path_for(self.set_root).read_bytes())
        self.assertEqual(parsed.findings, ())
        self.assertEqual(len(parsed.records), 2)
        self.assertEqual([section.section_type for section in parsed.records[0].record.sections], ["majority"])
        self.assertEqual(parsed.records[0].record.sections[0].end, len(self.text))

    def test_blank_and_unsettled_cases_are_neither_validated_nor_written(self) -> None:
        self.export()
        book = self.load()
        book["Cases"]["H2"] = " UNSETTLED "
        book["segmenter-001"]["B5"] = SENTINEL
        del book["segmenter-002"]
        book.save(self.path)
        (self.sheets / "segmenter-001.txt").unlink()
        before = self.snapshot()
        code, output, errors = self.import_()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, "checked 0, unsettled 1, not yet checked 1\n"
                         "unsettled: segmenter-001\nnot yet checked: segmenter-002\nwritten\n")
        self.assertEqual(self.snapshot(), before)

    def test_refusals_are_located_content_free_and_write_nothing(self) -> None:
        (self.sheets / "segmenter-001.full.txt").write_text(
            "0000\t" + "x" * 2501 + "\n\n0001\tFictitious dissent", encoding="utf-8",
        )
        self.export()
        self.checked()
        baseline_book = self.path.read_bytes()
        baseline_sheets = self.snapshot()
        scenarios = (
            ("verdict", "segmenter-001 row 2 Verdict: not-in-list"),
            ("mark", "segmenter-001 row 7 Mark: not-in-list"),
            ("continuation", "segmenter-001 row 6 Mark: continuation-edited"),
            ("tab", "segmenter-001 row 0 Para: tab-missing"),
            ("order", "segmenter-001 row 7 Para: out-of-order"),
            ("paragraph-id", "segmenter-001 row 7 Para: out-of-order"),
            ("count", "segmenter-001 row 4 Para: paragraph-count"),
            ("first", "segmenter-001 row 5 Mark: first-unmarked"),
            ("sheet", "segmenter-001 row 0 Para: sheet-missing"),
            ("sheet-token", "segmenter-001 row 0 Para: sheet-invalid"),
            ("sheet-count", "segmenter-001 row 4 Para: paragraph-count"),
            ("column", "segmenter-001 row 4 Mark: column-missing"),
            ("index-column", "Cases row 1 Verdict: column-missing"),
            ("index", "Cases row 0 Case: index-missing"),
            ("case-id", "Cases row 2 Case: case-id-invalid"),
        )
        for scenario, finding in scenarios:
            with self.subTest(scenario=scenario):
                self.path.write_bytes(baseline_book)
                for name, payload in baseline_sheets.items():
                    (self.sheets / name).write_bytes(payload)
                book = self.load()
                tab = book["segmenter-001"]
                # A valid edit in the other case must also remain unwritten.
                book["segmenter-002"]["B6"] = "concurrence"
                sheet = self.sheets / "segmenter-001.txt"
                if scenario == "verdict":
                    book["Cases"]["H2"] = SENTINEL
                elif scenario == "mark":
                    tab["B7"] = SENTINEL
                elif scenario == "continuation":
                    tab["B6"] = SENTINEL
                elif scenario == "tab":
                    del book["segmenter-001"]
                elif scenario == "order":
                    tab["A7"] = 9
                elif scenario == "paragraph-id":
                    tab["A7"] = SENTINEL
                elif scenario == "count":
                    tab.delete_rows(7)
                elif scenario == "first":
                    tab["B5"] = None
                elif scenario == "sheet":
                    sheet.unlink()
                elif scenario == "sheet-token":
                    sheet.write_bytes(sheet.read_bytes().replace(b"[majority]", SENTINEL.encode()))
                elif scenario == "sheet-count":
                    sheet.write_bytes(sheet.read_bytes().replace(b"paragraphs 2", b"paragraphs 9"))
                elif scenario == "column":
                    tab["B4"] = SENTINEL
                elif scenario == "index-column":
                    book["Cases"]["H1"] = SENTINEL
                elif scenario == "index":
                    del book["Cases"]
                elif scenario == "case-id":
                    book["Cases"]["A2"] = SENTINEL
                book.save(self.path)
                before = self.snapshot()
                code, output, errors = self.import_()
                self.assertEqual((code, output), (1, ""), errors)
                self.assertIn(finding, errors)
                self.assertIn("findings; nothing written. Fix:", errors)
                self.assertNotIn(SENTINEL, errors)
                self.assertNotIn("Fictitious majority", errors)
                self.assertEqual(self.snapshot(), before)

    def test_all_findings_collected_including_index_row_case_id(self) -> None:
        self.export()
        self.checked()
        book = self.load()
        book["Cases"]["H3"] = SENTINEL
        book["segmenter-001"]["B6"] = SENTINEL
        book.save(self.path)
        before = self.snapshot()
        code, output, errors = self.import_()
        self.assertEqual((code, output), (1, ""))
        self.assertEqual(errors.splitlines(), [
            "segmenter workbook: refused — 2 findings; nothing written. Fix: Correct the cells named below in the workbook, then import again.",
            "segmenter-002 row 3 Verdict: not-in-list",
            "segmenter-001 row 6 Mark: not-in-list",
        ])
        self.assertEqual(self.snapshot(), before)

    def test_export_input_refusals(self) -> None:
        baseline = self.snapshot()
        for scenario in ("empty", "full-missing", "count", "token", "header"):
            with self.subTest(scenario=scenario):
                for path in self.sheets.iterdir():
                    path.unlink()
                for name, payload in baseline.items():
                    (self.sheets / name).write_bytes(payload)
                if scenario == "empty":
                    for path in self.sheets.iterdir():
                        path.unlink()
                elif scenario == "full-missing":
                    (self.sheets / "segmenter-001.full.txt").unlink()
                elif scenario == "count":
                    (self.sheets / "segmenter-001.full.txt").write_text("0000\tFictitious", encoding="utf-8")
                else:
                    path = self.sheets / "segmenter-001.txt"
                    value = path.read_bytes()
                    value = value.replace(b"[majority]", SENTINEL.encode()) if scenario == "token" else SENTINEL.encode()
                    path.write_bytes(value)
                code, output, errors = _run(["export", str(self.sheets), str(self.path)])
                self.assertEqual((code, output), (1, ""))
                self.assertIn("refused —", errors)
                self.assertIn("Fix:", errors)
                self.assertNotIn(SENTINEL, errors)
                if scenario != "empty":
                    self.assertIn("segmenter-001", errors)
                self.assertFalse(self.path.exists())

    def test_export_output_refusals(self) -> None:
        self.export()
        before = self.path.read_bytes()
        code, output, errors = _run(["export", str(self.sheets), str(self.path)])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Choose another name so a checked workbook is never overwritten.", errors)
        self.assertEqual(self.path.read_bytes(), before)
        inside = Path(workbook.__file__).resolve().parent / "fictitious-review.xlsx"
        code, output, errors = _run(["export", str(self.sheets), str(inside)])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("output is inside the checkout", errors)
        self.assertFalse(inside.exists())

    def test_unexpected_exception_reports_class_alone(self) -> None:
        with patch.object(workbook, "export", side_effect=RuntimeError(SENTINEL)):
            self.assertEqual(_run(["export", str(self.sheets), str(self.path)]), (1, "", "segmenter workbook: internal error (RuntimeError)\n"))


class Command(unittest.TestCase):
    def test_usage_and_module_entry_exit_two(self) -> None:
        code, output, errors = _run([])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("usage:", errors)
        output_stream, error_stream = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["tools.segmenter.workbook"]), patch.dict(sys.modules), \
                redirect_stdout(output_stream), redirect_stderr(error_stream):
            del sys.modules["tools.segmenter.workbook"]
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("tools.segmenter.workbook", run_name="__main__")
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(output_stream.getvalue(), "")
        self.assertIn("usage:", error_stream.getvalue())

    def test_module_import_boundary(self) -> None:
        tree = ast.parse(Path(workbook.__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                modules = [entry.name for entry in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                self.assertTrue(module.split(".")[0] in sys.stdlib_module_names
                                or module.startswith(("gideon.", "tools.segmenter"))
                                or module == "tools.workbook"
                                or (module == "tools" and isinstance(node, ast.ImportFrom)
                                    and [entry.name for entry in node.names] == ["workbook"]), module)
