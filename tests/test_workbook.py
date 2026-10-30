"""A fictitious person's workbook round trip, with content-free refusals."""

import ast
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from tools import workbook

LONG_TEXT = "=Fictitious\x01 " + "word " * 600
SENTINEL = "FICTITIOUS_CELL_CONTENT_SENTINEL"


def _unbracket(text: str) -> str:
    return text.strip("[]").strip() or "-"


def _book() -> workbook.Book:
    columns = (
        workbook.Column("Number", width=7, number_format="0000"),
        workbook.Column("Choice", "list", ("-", "begin", "unknown"), baseline="Draft", width=21,
                        normalizer=_unbracket, error_title="Not a mark",
                        error="Pick a type from the list, or - to continue the section above."),
        workbook.Column("Draft", width=21, font_color="808080"),
        workbook.Column("Text", wrap=True),
    )
    return workbook.Book(
        "Guide", ("Fictitious checking guide", "", "The choices", "=literal guide\x02"),
        workbook.Index("Units", (
            workbook.Column("Unit", width=16),
            workbook.Column("Verdict", "list", ("approved", "pending"), width=12, allow_blank=True),
            workbook.Column("State", "list", ("ready", "waiting"), width=13),
            workbook.Column("Notes", "text", width=60),
            workbook.Column("Original", width=12),
        ), "Unit", (
            ("unit-a", "approved", "ready", "Fictitious note", "https://example.org/a"),
            ("unit-b", "", "waiting", "", ""),
        ), hyperlinks={"Original": "open"}),
        (
            workbook.Tab("unit-a", columns, "Number", (
                workbook.Cell(1, 1, "← Units", hyperlink="#'Units'!A1"),
                workbook.Cell(1, 2, "unit-a", bold=True),
                workbook.Cell(1, 4, "=Fictitious facts\x03"),
                workbook.Cell(1, 5, "Original", hyperlink="https://example.org/a"),
                workbook.Cell(2, 4, "Fictitious citation"),
            ), ((0, "begin", "begin", LONG_TEXT), (1, "-", "-", "Fictitious short text")),
                fills={"unknown": workbook.UNKNOWN_FILL}, start=("Choice", "-")),
            workbook.Tab("unit-b", columns, "Number", rows=((0, "unknown", "unknown", "Fictitious text"),)),
        ),
        guide_headings=("Fictitious checking guide", "The choices"),
    )


def _shape(book: workbook.Book) -> workbook.Book:
    return replace(book, index=replace(book.index, rows=()), tabs=tuple(replace(tab, rows=()) for tab in book.tabs))


class Workbook(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.path = self.root / "review.xlsx"
        self.book = _book()

    def export(self) -> None:
        self.assertEqual(workbook.export(self.book, self.path, checkout=self.checkout), (2, 3))

    def load(self):
        book = workbook.openpyxl.load_workbook(self.path)
        self.addCleanup(book.close)
        return book

    def test_export_refuses_before_writing(self) -> None:
        self.path.write_bytes(b"fictitious checked workbook")
        with self.assertRaises(workbook.WorkbookError) as caught:
            workbook.export(self.book, self.path, checkout=self.checkout)
        self.assertEqual(caught.exception.problem, "output exists")
        self.assertEqual(caught.exception.fix, "Choose another name so a checked workbook is never overwritten.")
        self.assertEqual(self.path.read_bytes(), b"fictitious checked workbook")
        for path in (self.checkout / "absent" / "new.xlsx", self.checkout / ".." / "checkout" / "new.xlsx"):
            with self.subTest(path=path), self.assertRaises(workbook.WorkbookError) as caught:
                workbook.export(self.book, path, checkout=self.checkout)
            self.assertEqual(caught.exception.problem, "output is inside the checkout")
            self.assertEqual(caught.exception.fix, "Choose a path outside the checkout.")
            self.assertFalse(path.exists())
        self.assertEqual(tuple(self.checkout.iterdir()), ())

    def test_export_resolves_symlinks_for_checkout_guard(self) -> None:
        alias = self.root / "alias"
        alias.symlink_to(self.checkout, target_is_directory=True)
        with self.assertRaises(workbook.WorkbookError) as caught:
            workbook.export(self.book, alias / "new.xlsx", checkout=self.checkout)
        self.assertEqual(caught.exception.problem, "output is inside the checkout")
        self.assertEqual(tuple(self.checkout.iterdir()), ())

    def test_guide_index_and_preamble(self) -> None:
        self.export()
        book = self.load()
        self.assertEqual(book.sheetnames, ["Guide", "Units", "unit-a", "unit-b"])
        self.assertEqual(book.active.title, "Guide")
        guide = book["Guide"]
        self.assertEqual([guide.cell(row, 1).value for row in range(1, 5)],
                         ["Fictitious checking guide", None, "The choices", "=literal guide "])
        self.assertEqual(guide.column_dimensions["A"].width, 130)
        self.assertTrue(guide["A1"].font.bold)
        self.assertTrue(guide["A3"].font.bold)
        self.assertFalse(guide["A4"].font.bold)
        self.assertEqual(guide["A4"].data_type, "s")
        index = book["Units"]
        self.assertEqual(tuple(cell.value for cell in index[1]), tuple(column.name for column in self.book.index.columns))
        self.assertTrue(all(cell.font.bold for cell in index[1]))
        self.assertEqual(index.freeze_panes, "A2")
        self.assertEqual(index.auto_filter.ref, "A1:E3")
        self.assertEqual(index["A2"].hyperlink.target, "#'unit-a'!A1")
        self.assertEqual(index["A2"].font.color.rgb, "000563C1")
        self.assertEqual(index["A2"].font.underline, "single")
        self.assertEqual(index["E2"].value, "open")
        self.assertEqual(index["E2"].hyperlink.target, self.book.index.rows[0][4])
        self.assertIsNone(index["E3"].hyperlink)
        tab = book["unit-a"]
        self.assertEqual(tab["A1"].value, "← Units")
        self.assertEqual(tab["A1"].hyperlink.target, "#'Units'!A1")
        self.assertTrue(tab["B1"].font.bold)
        self.assertEqual(tab["D1"].value, "=Fictitious facts ")
        self.assertEqual(tab["D1"].data_type, "s")
        self.assertEqual(tab["E1"].hyperlink.target, "https://example.org/a")
        self.assertEqual(tab["D2"].value, "Fictitious citation")

    def test_validation_styles_chunks_heights_and_conditions(self) -> None:
        self.export()
        book = self.load()
        index_rules = book["Units"].data_validations.dataValidation
        self.assertEqual([(rule.formula1, str(rule.sqref), rule.allow_blank) for rule in index_rules],
                         [('"approved,pending"', "B2 B3", True), ('"ready,waiting"', "C2 C3", False)])
        self.assertTrue(all(not rule.showErrorMessage for rule in index_rules))
        tab = book["unit-a"]
        self.assertEqual(tuple(cell.value for cell in tab[4][:4]), ("Number", "Choice", "Draft", "Text"))
        self.assertEqual(tab.freeze_panes, "B5")
        self.assertEqual([tab.column_dimensions[letter].width for letter in "ABCD"], [7, 21, 21, 110])
        self.assertEqual(tab["A5"].number_format, "0000")
        self.assertEqual(tab["A5"].alignment.vertical, "top")
        self.assertEqual(tab["B5"].alignment.vertical, "top")
        self.assertEqual(tab["C5"].font.color.rgb, "00808080")
        self.assertEqual(tab["A5"].font.color.type, "theme")
        self.assertEqual(tab["D5"].alignment.vertical, "top")
        self.assertTrue(tab["D5"].alignment.wrap_text)
        self.assertEqual(tab["D5"].value, "=Fictitious  " + "word " * 496 + "word")
        self.assertEqual(tab["D5"].data_type, "s")
        self.assertEqual(tab["D6"].value, "… " + "word " * 103)
        self.assertEqual([tab.cell(6, col).value for col in (1, 2, 3)], [None, None, None])
        self.assertEqual(tab["A7"].value, 1)
        self.assertEqual(tab.row_dimensions[5].height, 362)
        self.assertEqual(tab.row_dimensions[6].height, 77)
        self.assertEqual(tab.row_dimensions[7].height, 17)
        rules = tab.data_validations.dataValidation
        self.assertEqual(len(rules), 1)
        self.assertEqual(str(rules[0].sqref), "B5 B7")
        self.assertEqual(rules[0].formula1, '"-,begin,unknown"')
        self.assertFalse(rules[0].allow_blank)
        self.assertTrue(rules[0].showErrorMessage)
        self.assertEqual(rules[0].errorTitle, "Not a mark")
        self.assertEqual(rules[0].error, "Pick a type from the list, or - to continue the section above.")
        conditions = [(str(span.sqref), rule) for span in tab.conditional_formatting
                      for rule in tab.conditional_formatting[span]]
        self.assertEqual([(span, rule.formula) for span, rule in conditions], [
            ("A5:D7", ['$B5="unknown"']),
            ("A5:D7", ['AND($B5<>"",$B5<>"-")']),
            ("B5:B7", ['AND($A5<>"",$B5<>$C5)']),
        ])
        self.assertEqual(conditions[0][1].dxf.fill.fgColor.rgb, "00FCE4D6")
        self.assertEqual(conditions[1][1].dxf.fill.fgColor.rgb, "00DDEBF7")
        self.assertTrue(conditions[2][1].dxf.font.bold)
        self.assertEqual(conditions[2][1].dxf.font.color.rgb, "00C00000")

    def test_custom_layout_hard_split_and_height_cap(self) -> None:
        tab = replace(self.book.tabs[0], rows=((0, "begin", "begin", "x" * 10001),))
        book = replace(self.book, tabs=(tab,), header_row=6, chunk_size=10000)
        self.assertEqual(workbook.export(book, self.path, checkout=self.checkout), (1, 1))
        read = self.load()["unit-a"]
        self.assertEqual(read.freeze_panes, "B7")
        self.assertEqual(read["D7"].value, "x" * 10000)
        self.assertEqual(read["D8"].value, "… x")
        self.assertIsNone(read["A8"].value)
        self.assertEqual(read.row_dimensions[7].height, 409)
        self.assertEqual(read.row_dimensions[8].height, 17)
        self.assertEqual(workbook.import_(self.path, _shape(book)).tab_rows["unit-a"], {
            7: {"Number": 0, "Choice": "begin"},
        })

    def test_import_untouched_values_and_no_writes(self) -> None:
        self.export()
        before = self.path.read_bytes()
        result = workbook.import_(self.path, _shape(self.book))
        self.assertEqual(result.findings, ())
        self.assertEqual(result.index_rows, {
            2: {"Unit": "unit-a", "Verdict": "approved", "State": "ready", "Notes": "Fictitious note"},
            3: {"Unit": "unit-b", "Verdict": "", "State": "waiting", "Notes": ""},
        })
        self.assertEqual(result.tab_rows, {
            "unit-a": {5: {"Number": 0, "Choice": "begin"}, 7: {"Number": 1, "Choice": "-"}},
            "unit-b": {5: {"Number": 0, "Choice": "unknown"}},
        })
        self.assertEqual(self.path.read_bytes(), before)

    def test_all_findings_are_located_and_content_free(self) -> None:
        self.export()
        book = self.load()
        book["Units"]["B2"] = SENTINEL
        book["unit-a"]["B5"] = SENTINEL
        book["unit-a"]["B6"] = SENTINEL
        del book["unit-b"]
        book.save(self.path)
        before = self.path.read_bytes()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = workbook.import_(self.path, _shape(self.book))
        self.assertEqual(result.findings, (
            workbook.Finding("Units", 2, "Verdict", workbook.NOT_IN_LIST),
            workbook.Finding("unit-a", 5, "Choice", workbook.NOT_IN_LIST),
            workbook.Finding("unit-a", 6, "Choice", workbook.CONTINUATION_EDITED),
            workbook.Finding("unit-b", 0, "Number", workbook.TAB_MISSING),
        ))
        for value in (repr(result), repr(result.findings), str(result.findings), output.getvalue(), errors.getvalue()):
            self.assertNotIn(SENTINEL, value)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_index(self) -> None:
        self.export()
        book = self.load()
        del book["Units"]
        book.save(self.path)
        result = workbook.import_(self.path, _shape(self.book))
        self.assertEqual(result, workbook.Imported({}, {}, (
            workbook.Finding("Units", 0, "Unit", workbook.INDEX_MISSING),
        )))

    def test_normalization_and_explicit_tab_selection(self) -> None:
        self.export()
        book = self.load()
        book["Units"]["B2"] = "  APPROVED  "
        book["Units"]["D2"] = "  A Fictitious Note  "
        book["unit-a"]["B5"] = " [BEGIN] "
        book["unit-a"]["B7"] = None
        book["unit-b"]["B5"] = SENTINEL
        book.save(self.path)
        shape = _shape(self.book)
        result = workbook.import_(self.path, replace(shape, tabs=shape.tabs[:1]))
        self.assertEqual(result.findings, ())
        self.assertEqual(result.index_rows[2]["Verdict"], "approved")
        self.assertEqual(result.index_rows[2]["Notes"], "A Fictitious Note")
        self.assertEqual(result.tab_rows["unit-a"][5]["Choice"], "begin")
        self.assertEqual(result.tab_rows["unit-a"][7]["Choice"], "-")
        self.assertEqual(tuple(result.tab_rows), ("unit-a",))

    def test_renamed_heading_is_a_finding(self) -> None:
        self.export()
        book = self.load()
        book["Units"]["B1"] = SENTINEL
        book["unit-a"]["A4"] = SENTINEL
        book.save(self.path)
        result = workbook.import_(self.path, _shape(self.book))
        self.assertEqual(result.findings, (
            workbook.Finding("Units", 1, "Verdict", workbook.COLUMN_MISSING),
            workbook.Finding("unit-a", 4, "Number", workbook.COLUMN_MISSING),
        ))
        self.assertNotIn(SENTINEL, repr(result))

    def test_index_ids_default_to_missing_tab_checks(self) -> None:
        self.export()
        book = self.load()
        del book["unit-b"]
        book.save(self.path)
        result = workbook.import_(self.path, replace(_shape(self.book), tabs=()))
        self.assertEqual(result.findings, (workbook.Finding("unit-b", 0, "Unit", workbook.TAB_MISSING),))

    def test_headings_determine_import_positions(self) -> None:
        self.export()
        book = self.load()
        for row in book["Units"].iter_rows():
            row[1].value, row[3].value = row[3].value, row[1].value
        for row in book["unit-a"].iter_rows(min_row=4, max_col=4):
            row[1].value, row[2].value = row[2].value, row[1].value
        book["unit-a"]["C5"] = "unknown"
        book.save(self.path)
        result = workbook.import_(self.path, _shape(self.book))
        self.assertEqual(result.findings, ())
        self.assertEqual(result.index_rows[2]["Notes"], "Fictitious note")
        self.assertEqual(result.index_rows[2]["Verdict"], "approved")
        self.assertEqual(result.tab_rows["unit-a"][5]["Choice"], "unknown")

    def test_exports_have_equal_values(self) -> None:
        self.export()
        second_path = self.root / "second.xlsx"
        workbook.export(self.book, second_path, checkout=self.checkout)
        first = self.load()
        second = workbook.openpyxl.load_workbook(second_path)
        self.addCleanup(second.close)
        self.assertEqual(first.sheetnames, second.sheetnames)
        for name in first.sheetnames:
            with self.subTest(tab=name):
                self.assertEqual(tuple(first[name].values), tuple(second[name].values))


class Imports(unittest.TestCase):
    def test_only_layer_imports_openpyxl_in_tools(self) -> None:
        root = Path(__file__).resolve().parents[1]
        found = set()
        for path in (root / "tools").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [entry.name for entry in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                if any(module.split(".")[0] == "openpyxl" for module in modules):
                    found.add(path.relative_to(root).as_posix())
                    self.assertIn(node, tree.body)
        self.assertEqual(found, {"tools/workbook.py"})
