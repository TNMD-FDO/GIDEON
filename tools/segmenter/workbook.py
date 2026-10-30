"""Export segmenter review sheets and validate returned workbook marks."""

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from gideon.host.render.worker import SECTION_TYPES
from tools import workbook
from tools.segmenter import manifest

CONTINUE = "-"
MARKS = (CONTINUE, *SECTION_TYPES)
VERDICTS = ("checked", "unsettled")
CASE_TAB = re.compile(r"segmenter-[0-9]{3}")
LINE = re.compile(r"([0-9]{4,}) (\[[^\]]*\]|-)(.*)")
OUT_OF_ORDER = "out-of-order"
PARAGRAPH_COUNT = "paragraph-count"
FIRST_UNMARKED = "first-unmarked"
SHEET_MISSING = "sheet-missing"
SHEET_INVALID = "sheet-invalid"
CASE_ID_INVALID = "case-id-invalid"

GUIDE = (
    "How to check a case",
    "",
    "1. On the Cases tab, click a case id to open its tab.",
    "2. Each row is one paragraph. The Mark column is the only thing you edit (use the dropdown).",
    "   A type (majority, dissent, footnote, ...) means a new section STARTS at that paragraph.",
    "   - means the paragraph continues the section above.",
    "3. Fix a wrong type by picking the right one; remove a wrong start by picking -;",
    "   add a missed start by picking its type on that paragraph.",
    "4. The first paragraph (0000) must always carry a type.",
    "5. Blue rows are section starts; orange rows are 'unknown'; a red bold mark is one you changed from the draft.",
    "6. Rows with no paragraph number continue a long paragraph's text; leave their Mark empty.",
    "7. Use the 'Original' link (CourtListener) for footnotes and bylines the plain text lost.",
    "8. When a case is done, set its Verdict on the Cases tab to 'checked'.",
    "   If you cannot settle it, set 'unsettled' and leave its marks alone. Never guess.",
    "",
    "The rules (eval/sets/eval-v1/build-gates/README.md, 'The label guide' and 'The check')",
    "",
    "- Mark what the text itself states, not what you know about the case.",
    "- A byline or heading begins an opinion section; a bare byline that begins no new opinion is -.",
    "- Each footnote body starts its own footnote section, wherever the text placed it.",
    "- A record holding one separate opinion alone (a dissent filed as its own opinion) carries that type from 0000.",
    "- unknown: the text itself does not establish a type.",
    "",
    "The types",
    "",
    "syllabus — an official case summary begins",
    "headmatter — caption, case details, or a participation line before the opinion",
    "majority / plurality / per_curiam — a byline or heading begins the court's opinion of that kind",
    "concurrence / dissent / concurrence_dissent — a separate opinion begins at its byline or heading",
    "footnote — a footnote body begins; each footnote is its own section",
    "appendix — appended material begins",
    "order — a court order begins",
    "unknown — the text itself does not establish a type",
)


@dataclass(frozen=True)
class Sheet:
    """Parsed sheet facts and the original lines needed for token replacement."""

    court: str
    paragraphs: int
    status: str
    cited: str = field(repr=False)
    original: str = field(repr=False)
    marks: tuple[str, ...] = field(repr=False)
    lines: tuple[str, ...] = field(repr=False)


class Refusal(Exception):
    """A content-free refusal, its fix, and any located findings."""

    def __init__(self, problem: str, fix: str, findings: Sequence[workbook.Finding] = ()) -> None:
        super().__init__(problem)
        self.problem = problem
        self.fix = fix
        self.findings = tuple(findings)


def read_sheet(path: Path) -> Sheet:
    """Read sheet fields and mark tokens while retaining every original line ending."""
    try:
        lines = tuple(path.read_bytes().decode("utf-8").splitlines(keepends=True))
        head = dict(part.strip().split(" ", 1) for part in lines[0].split("|"))
        paragraphs = int(head["paragraphs"])
        court, status = head["court"], head["status"]
        plain = tuple(line.rstrip("\r\n") for line in lines)
        start = plain.index(manifest.SHEET_PARAGRAPHS_HEADER) + 1
    except (OSError, UnicodeError, ValueError, KeyError, IndexError) as error:
        raise Refusal(f"{path.stem}: sheet cannot be read", "Restore a readable segmenter draft sheet, then retry.") from error
    cited, original = "", ""
    for line in plain:
        if line.startswith("Cited as: "):
            cited = line.removeprefix("Cited as: ")
        elif line.startswith("Original: "):
            original = line.removeprefix("Original: ")
    marks = []
    for line in plain[start:]:
        if not line.strip():
            continue
        match = LINE.fullmatch(line)
        if match is None:
            raise Refusal(f"{path.stem}: a paragraph line has no mark token", "Restore the paragraph mark token, then retry.")
        token = match.group(2)
        marks.append(CONTINUE if token == CONTINUE else token[1:-1])
    return Sheet(court, paragraphs, status, cited, original, tuple(marks), lines)


def read_full(path: Path) -> list[str]:
    """Read the draft's full-text blocks, taking the text after each first tab."""
    try:
        blocks = path.read_text(encoding="utf-8").split("\n\n")
    except (OSError, UnicodeError) as error:
        case_id = path.name.removesuffix(".full.txt")
        raise Refusal(f"{case_id}: full text cannot be read", "Restore the draft's full-text file, then retry.") from error
    return [block.split("\t", 1)[1] if "\t" in block else "" for block in blocks if block.strip()]


def normalized(text: str) -> str:
    """Strip the brackets tolerated by the hand tool, mapping blank to continuation."""
    return text.strip("[]").strip() or CONTINUE


def _tab(case_id: str) -> workbook.Tab:
    return workbook.Tab(case_id, (
        workbook.Column("Para", width=7, number_format="0000"),
        workbook.Column("Mark", "list", MARKS, baseline="Draft", width=21, normalizer=normalized,
                        error_title="Not a mark",
                        error="Pick a type from the list, or - to continue the section above."),
        workbook.Column("Draft", width=21, font_color="808080"),
        workbook.Column("Text", wrap=True),
    ), "Para", fills={"unknown": workbook.UNKNOWN_FILL}, start=("Mark", CONTINUE))


def _book() -> workbook.Book:
    return workbook.Book("Start here", GUIDE, workbook.Index("Cases", (
        workbook.Column("Case", width=16),
        workbook.Column("Court", width=8),
        workbook.Column("Paragraphs", width=12),
        workbook.Column("Draft status", width=13),
        workbook.Column("Draft starts", width=13),
        workbook.Column("Draft unknown", width=14),
        workbook.Column("Original", width=12),
        workbook.Column("Verdict", "list", VERDICTS, width=12, allow_blank=True),
        workbook.Column("Notes", "text", width=60),
    ), "Case", hyperlinks={"Original": "open"}), guide_headings=(
        "How to check a case", "The types",
        "The rules (eval/sets/eval-v1/build-gates/README.md, 'The label guide' and 'The check')",
    ))


def export(sheets: Path, out: Path, *, stdout: TextIO) -> None:
    """Declare and export the hand tool's workbook layout over the draft sheets."""
    ids = sorted(path.stem for path in sheets.glob("segmenter-*.txt") if CASE_TAB.fullmatch(path.stem))
    if not ids:
        raise Refusal("no segmenter sheets", "Provide a directory of segmenter draft sheets, then retry.")
    index_rows: list[tuple[object, ...]] = []
    tabs = []
    for case_id in ids:
        sheet = read_sheet(sheets / f"{case_id}.txt")
        full = read_full(sheets / f"{case_id}.full.txt")
        if len(full) != sheet.paragraphs or len(sheet.marks) != sheet.paragraphs:
            raise Refusal(f"{case_id}: sheet and full text disagree on the paragraph count",
                          "Restore matching draft sheet and full-text files, then retry.")
        index_rows.append((case_id, sheet.court, sheet.paragraphs, sheet.status,
                           sum(mark != CONTINUE for mark in sheet.marks), sheet.marks.count("unknown"),
                           sheet.original or None, None, None))
        preamble = [
            workbook.Cell(1, 1, "← Cases", hyperlink="#'Cases'!A1"),
            workbook.Cell(1, 2, case_id, bold=True),
            workbook.Cell(1, 4, f"court {sheet.court} · {sheet.paragraphs} paragraphs · draft {sheet.status}"),
            workbook.Cell(2, 4, f"Cited as: {sheet.cited}"),
        ]
        if sheet.original:
            preamble.append(workbook.Cell(1, 5, "Original (CourtListener)", hyperlink=sheet.original))
        tabs.append(replace(_tab(case_id), preamble=tuple(preamble), rows=tuple(
            (paragraph, mark, mark, text)
            for paragraph, (mark, text) in enumerate(zip(sheet.marks, full, strict=True))
        )))
    book = _book()
    count, _ = workbook.export(replace(book, index=replace(book.index, rows=tuple(index_rows)), tabs=tuple(tabs)),
                               out, checkout=Path(__file__).resolve().parents[2])
    print(f"exported {count} cases to {out}", file=stdout)


def _rewrite(sheet: Sheet, marks: Sequence[str]) -> bytes:
    lines = list(sheet.lines)
    start = next(i for i, line in enumerate(lines) if line.rstrip("\r\n") == manifest.SHEET_PARAGRAPHS_HEADER) + 1
    paragraph = 0
    for index in range(start, len(lines)):
        match = LINE.fullmatch(lines[index].rstrip("\r\n"))
        if match is None:
            continue
        mark = marks[paragraph]
        token = CONTINUE if mark == CONTINUE else f"[{mark}]"
        lines[index] = lines[index][:match.start(2)] + token + lines[index][match.end(2):]
        paragraph += 1
    return "".join(lines).encode("utf-8")


def _paragraph_order(value: object, expected: int) -> bool:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return False
    try:
        return int(value) == expected and (not isinstance(value, float) or value == expected)
    except (ValueError, OverflowError):
        return False


def import_(path: Path, sheets: Path, *, dry_run: bool, stdout: TextIO) -> None:
    """Validate every checked case before replacing any sheet's mark tokens."""
    shape = _book()
    index = workbook.import_(path, shape)
    identities = {row: str(values["Case"]).strip() for row, values in index.index_rows.items()
                  if CASE_TAB.fullmatch(str(values["Case"]).strip())}
    findings = [replace(finding, tab=identities.get(finding.row, "Cases"))
                for finding in index.findings if finding.tab == "Cases"]
    findings.extend(workbook.Finding("Cases", row, "Case", CASE_ID_INVALID)
                    for row in index.index_rows if row not in identities)
    verdicts = {identities[row]: str(values["Verdict"]) for row, values in index.index_rows.items() if row in identities}
    checked = sorted(case for case, verdict in verdicts.items() if verdict == "checked")
    unsettled = sorted(case for case, verdict in verdicts.items() if verdict == "unsettled")
    open_ = sorted(case for case, verdict in verdicts.items() if not verdict)
    tab_rows: Mapping[str, Mapping[int, Mapping[str, object]]] = {}
    if checked:
        imported = workbook.import_(path, replace(shape, tabs=tuple(_tab(case) for case in checked)))
        findings.extend(finding for finding in imported.findings if finding.tab != "Cases")
        tab_rows = imported.tab_rows
    rewrites: dict[str, tuple[bytes, int]] = {}
    for case_id in checked:
        sheet_path = sheets / f"{case_id}.txt"
        if not sheet_path.is_file():
            findings.append(workbook.Finding(case_id, 0, "Para", SHEET_MISSING))
            continue
        try:
            sheet = read_sheet(sheet_path)
        except Refusal:
            findings.append(workbook.Finding(case_id, 0, "Para", SHEET_INVALID))
            continue
        if case_id not in tab_rows or any(f.tab == case_id and f.kind == workbook.COLUMN_MISSING for f in findings):
            continue
        rows = tab_rows[case_id]
        marks = []
        for expected, (row, values) in enumerate(rows.items()):
            if not _paragraph_order(values["Para"], expected):
                findings.append(workbook.Finding(case_id, row, "Para", OUT_OF_ORDER))
                break
            marks.append(str(values["Mark"]))
        if len(marks) != sheet.paragraphs or len(sheet.marks) != sheet.paragraphs:
            findings.append(workbook.Finding(case_id, shape.header_row, "Para", PARAGRAPH_COUNT))
            continue
        if marks and marks[0] == CONTINUE:
            findings.append(workbook.Finding(case_id, next(iter(rows)), "Mark", FIRST_UNMARKED))
        changed = sum(new != old for new, old in zip(marks, sheet.marks, strict=True))
        rewrites[case_id] = (_rewrite(sheet, marks), changed)
    if findings:
        raise Refusal(f"{len(findings)} findings; nothing written",
                      "Correct the cells named below in the workbook, then import again.", findings)
    for case_id, (payload, changed) in rewrites.items():
        if not dry_run:
            manifest.write_atomic(sheets / f"{case_id}.txt", payload)
        print(f"{case_id}: {changed} marks changed from the sheet", file=stdout)
    print(f"checked {len(checked)}, unsettled {len(unsettled)}, not yet checked {len(open_)}", file=stdout)
    print("unsettled: " + (", ".join(unsettled) or "none"), file=stdout)
    if open_:
        print("not yet checked: " + ", ".join(open_), file=stdout)
    print("dry run: nothing written" if dry_run else "written", file=stdout)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    out = commands.add_parser("export")
    out.add_argument("sheets", type=Path)
    out.add_argument("workbook", type=Path)
    back = commands.add_parser("import")
    back.add_argument("workbook", type=Path)
    back.add_argument("sheets", type=Path)
    back.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    """Run the workbook command with content-free diagnostics and return its status."""
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    try:
        with redirect_stdout(output), redirect_stderr(errors):
            args = _parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        if args.command == "export":
            export(args.sheets, args.workbook, stdout=output)
        else:
            import_(args.workbook, args.sheets, dry_run=args.dry_run, stdout=output)
    except (Refusal, workbook.WorkbookError) as error:
        print(f"segmenter workbook: refused — {error.problem}. Fix: {error.fix}", file=errors)
        if isinstance(error, Refusal):
            for finding in error.findings:
                print(f"{finding.tab} row {finding.row} {finding.column}: {finding.kind}", file=errors)
        return 1
    except Exception as error:  # noqa: BLE001 - command boundary keeps workbook and sheet text out of diagnostics
        print(f"segmenter workbook: internal error ({type(error).__name__})", file=errors)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
