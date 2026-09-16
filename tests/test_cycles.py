"""The cycles record: the parse, the bands, the merge, the refusals, the hygiene."""

import ast
import json
import sys
from collections.abc import Iterable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tools import cycles

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = Path("/repo/GIDEON")
SENSITIVE_PROMPT_SENTENCE = "FICTITIOUS_PROMPT_SENTENCE_MUST_NOT_LEAK"
PRIMARY_CWD = str(CHECKOUT)
WORKTREE_CWD = str(CHECKOUT / ".claude" / "worktrees" / "cycle-name")


def assistant(
    message_id: str,
    timestamp: str,
    values: tuple[int, int, int, int],
    *,
    sidechain: bool = False,
    model: str | None = None,
    effort: str | None = None,
    tool_skill: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "id": message_id,
        "usage": dict(zip(cycles.USAGE_FIELDS, values, strict=True)),
    }
    if model is not None:
        message["model"] = model
    if tool_skill is not None:
        message["content"] = [{"type": "tool_use", "name": "Skill", "input": {"skill": tool_skill}}]
    entry: dict[str, object] = {"type": "assistant", "timestamp": timestamp, "message": message}
    if effort is not None:
        entry["effort"] = effort
    if sidechain:
        entry["isSidechain"] = True
    return entry


def user(timestamp: str, content: object, cwd: str) -> dict[str, object]:
    return {"type": "user", "timestamp": timestamp, "cwd": cwd, "message": {"content": content}}


def write_jsonl(path: Path, entries: Iterable[object]) -> None:
    """Write fixture entries, a string entry standing for one non-JSON line."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [entry if isinstance(entry, str) else json.dumps(entry) for entry in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_cli(arguments: Sequence[str], checkout: Path, projects: Path) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cycles.main(arguments, checkout=checkout, projects=projects)
    return code, stdout.getvalue(), stderr.getvalue()


class Fixture:
    """A synthetic store: the primary's directory, one worktree's, and a sibling's."""

    def __init__(self, directory: Path) -> None:
        self.projects = directory / "projects"
        slug = cycles.path_slug(CHECKOUT)
        self.primary = self.projects / slug
        self.worktree = self.projects / (slug + cycles.WORKTREE_MARKER + "cycle-name")
        self.primary_session = self.primary / "primary-session.jsonl"
        self.worktree_session = self.worktree / "worktree-session.jsonl"
        write_jsonl(
            self.primary_session,
            [
                user(
                    "2026-01-01T00:00:00.123Z",
                    "<command-name>/model</command-name> <command-name>/TRIP-1-plan</command-name> "
                    ".scratch/slice-1/issues/12-ticket.md " + SENSITIVE_PROMPT_SENTENCE,
                    PRIMARY_CWD,
                ),
                assistant("one", "2026-01-01T00:00:01Z", (10, 20, 30, 4), model="model-a", effort="high"),
                assistant("one", "2026-01-01T00:00:01Z", (10, 20, 30, 4), model="model-a", effort="high"),
                user(
                    "2026-01-01T00:00:02Z",
                    [{"type": "text", "text": "docs/1-plans/F_0.1.7_synthetic.plan.md and by-slug.plan.md"}],
                    WORKTREE_CWD,
                ),
                assistant("two", "2026-01-01T00:00:03Z", (100, 200, 300, 7), model="<synthetic>"),
                assistant("side", "2026-01-01T00:00:04Z", (900, 900, 900, 900), sidechain=True),
                {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-01T00:00:05Z"},
                "this line is not JSON",
            ],
        )
        write_jsonl(
            self.worktree_session,
            [
                user("2026-01-02T00:00:00Z", "<command-name>/triage</command-name>", PRIMARY_CWD),
                assistant("worktree-response", "2026-01-02T00:00:01Z", (5, 5, 5, 2), effort="medium"),
            ],
        )
        write_jsonl(self.primary / "stub-session.jsonl", ["not JSON", user("", "no usage", PRIMARY_CWD)])
        write_jsonl(
            self.projects / (slug + "-sibling-checkout") / "sibling-session.jsonl",
            [assistant("sibling", "2026-01-04T00:00:00Z", (999, 999, 999, 999))],
        )


class TranscriptParsing(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))

    def test_row_folds_responses_and_reads_ids_only(self) -> None:
        row = cycles.read_transcript(self.fixture.primary_session, CHECKOUT, "(primary)")
        assert row is not None
        self.assertEqual(row.short_id, "primary-")
        self.assertEqual(row.started, "2026-01-01 00:00")
        self.assertEqual(row.where, "worktree cycle-name")
        # The first TRIP skill names the row, not the first command.
        self.assertEqual(row.skill, "TRIP-1-plan")
        self.assertEqual(row.models, ("model-a",))
        self.assertEqual(row.efforts, ("high",))
        self.assertEqual(row.tickets, ("slice-1/12",))
        self.assertEqual(row.plans, ("0.1.7", "by-slug"))
        self.assertEqual((row.turns, row.peak, row.compactions), (2, 600, 1))
        self.assertEqual(row.bands, ("slice-1",))

    def test_tool_use_names_the_skill_and_a_ticketless_cycle_is_unattributed(self) -> None:
        path = self.fixture.primary / "tool-session.jsonl"
        write_jsonl(
            path,
            [
                user("2026-01-03T00:00:00Z", "no command", WORKTREE_CWD),
                assistant("a", "2026-01-03T00:00:01Z", (1, 0, 0, 1), tool_skill="a-slug:TRIP-2-implement"),
            ],
        )
        row = cycles.read_transcript(path, CHECKOUT, "(primary)")
        assert row is not None
        self.assertEqual(row.skill, "TRIP-2-implement")
        self.assertEqual(row.bands, ("unattributed",))

    def test_family_read_counts_stubs_and_malformed_and_skips_the_sibling(self) -> None:
        differing = self.fixture.primary / "differing-repeat.jsonl"
        write_jsonl(
            differing,
            [
                assistant("repeat", "2026-01-06T00:00:00Z", (1, 2, 3, 4)),
                assistant("repeat", "2026-01-06T00:00:01Z", (1, 2, 3, 5)),
            ],
        )
        rows, report = cycles.read_family(CHECKOUT, self.fixture.projects)
        self.assertEqual([row.id for row in rows], ["primary-session", "worktree-session"])
        self.assertEqual(rows[1].where, "(primary)")
        self.assertEqual((report.transcripts_read, report.without_usage), (4, 1))
        self.assertEqual(report.malformed_paths, (differing,))

    def test_primary_checkout_resolves_a_worktree(self) -> None:
        root = Path(self.temporary.name)
        (root / "primary" / ".git" / "worktrees").mkdir(parents=True)
        (root / "worktree").mkdir()
        (root / "worktree" / ".git").write_text(
            f"gitdir: {root / 'primary' / '.git' / 'worktrees' / 'cycle-name'}\n", encoding="utf-8"
        )
        self.assertEqual(cycles.primary_checkout(root / "worktree"), (root / "primary").resolve())
        self.assertEqual(cycles.primary_checkout(root / "primary"), (root / "primary").resolve())


def session(
    identifier: str,
    peak: int,
    turns: int = 1,
    skill: str = "",
    tickets: tuple[str, ...] = (),
    compactions: int = 0,
) -> cycles.Session:
    return cycles.Session(
        id=identifier,
        started=f"2026-02-01 00:{peak % 60:02d}",
        where="(primary)",
        skill=skill,
        models=(),
        efforts=(),
        tickets=tickets,
        plans=(),
        turns=turns,
        peak=peak,
        compactions=compactions,
    )


class Bands(TestCase):
    def test_bands_come_from_ticket_directories_and_the_two_lines(self) -> None:
        rows = cycles.band_rows(
            (
                session("slice", 900001, 151, "TRIP-2-implement", tickets=("slice-1/12",)),
                session("two", 810000, 200, "TRIP-1-plan", tickets=("slice-1/14", "workflow/06"), compactions=1),
                session("bare", 500000, 3, "TRIP-3-release"),
                session("triage", 100, 3, "triage", tickets=("workflow/02",)),
                session("plain", 200, 150),
            )
        )
        by_name = {cells[0]: cells[1:] for cells in rows}
        self.assertEqual(
            list(by_name),
            [
                "slice-1 cycle sessions",
                "workflow cycle sessions",
                "unattributed cycle sessions",
                "triage sessions",
                "every session",
            ],
        )
        self.assertEqual(by_name["slice-1 cycle sessions"], ("2", "855,000", "900,001", "2", "175", "200", "2", "1"))
        self.assertEqual(by_name["every session"], ("5", "500,000", "900,001", "2", "150", "200", "2", "1"))
        self.assertEqual(by_name["triage sessions"][0], "1")


class CommandLine(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))

    def assert_refusal(self, arguments: Sequence[str], expected: str, projects: Path | None = None) -> None:
        code, stdout, stderr = run_cli(arguments, CHECKOUT, projects or self.fixture.projects)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertRegex(stderr, r"^python3 -m tools\.cycles: .+ Fix: .+\n$")
        self.assertIn(expected, stderr)

    def test_no_store_refusal_names_the_store(self) -> None:
        empty = Path(self.temporary.name) / "empty"
        empty.mkdir()
        self.assert_refusal([], "--projects", projects=empty)

    def test_tables_carry_no_prompt_text(self) -> None:
        code, stdout, stderr = run_cli([], CHECKOUT, self.fixture.projects)
        self.assertEqual((code, stderr), (0, ""))
        for heading in (cycles.BANDS_HEADING, cycles.SESSIONS_HEADING, "## Report"):
            self.assertIn(heading, stdout)
        self.assertIn("transcripts without usage: 1", stdout)
        self.assertNotIn(SENSITIVE_PROMPT_SENTENCE, stdout)

    def test_latest_prints_one_line_for_the_seam(self) -> None:
        code, stdout, stderr = run_cli(["--latest"], CHECKOUT, self.fixture.projects)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(
            stdout,
            f"latest session worktree ((primary), triage): 1 turns of {cycles.SEAM_TURNS}, "
            f"peak 15 of {cycles.EARLY_WARNING:,}, model -, effort medium\n",
        )
        code, stdout, _ = run_cli(["--latest", "--session", "primary"], CHECKOUT, self.fixture.projects)
        self.assertEqual(code, 0)
        self.assertIn("latest session primary- (worktree cycle-name, TRIP-1-plan): 2 turns", stdout)
        self.assertIn("model model-a, effort high", stdout)

    def test_selector_refusals(self) -> None:
        self.assert_refusal(["--latest", "--session", "missing"], "no present session id starts with")
        self.assert_refusal(["--latest", "--session", ""], "more than one present session id starts with")
        self.assert_refusal(["--session", "primary"], "--session can only be used with --latest")
        empty_family = Path(self.temporary.name) / "no-usage"
        write_jsonl(empty_family / cycles.path_slug(CHECKOUT) / "stub.jsonl", ["not JSON"])
        self.assert_refusal(["--latest"], "without --latest", projects=empty_family)


class Record(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))
        self.record = Path(self.temporary.name) / "record" / "CYCLES.md"

    def write(self) -> str:
        code, stdout, stderr = run_cli(["--write", str(self.record)], CHECKOUT, self.fixture.projects)
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(f"wrote {self.record.resolve()}:", stdout)
        return self.record.read_text(encoding="utf-8")

    def test_write_is_byte_stable_and_names_the_former_record(self) -> None:
        first = self.write()
        self.assertIn("| " + " | ".join(cycles.SESSION_COLUMNS) + " |", first)
        self.assertIn("| 2026-01-01 00:00 | primary- | worktree cycle-name | TRIP-1-plan | model-a | high |", first)
        self.assertIn("42-cycles-record-to-v0.1.61.md", first)
        self.assertNotIn(SENSITIVE_PROMPT_SENTENCE, first)
        self.assertEqual(self.write(), first)

    def test_merge_keeps_absent_rows_and_rewrites_present_rows(self) -> None:
        first = self.write().splitlines()
        kept_row = next(line for line in first if "| worktree |" in line)
        text = self.fixture.primary_session.read_text(encoding="utf-8")
        self.fixture.primary_session.write_text(text.replace('"input_tokens": 100', '"input_tokens": 300'))
        self.fixture.worktree_session.unlink()
        write_jsonl(self.fixture.worktree / "new-session.jsonl", [assistant("n", "2026-01-01T12:00:00Z", (8, 8, 8, 2))])
        second = self.write().splitlines()
        self.assertIn(kept_row, second)
        ids = [cycles._cells(line)[1] for line in second if line.startswith("| 2026")]
        self.assertEqual(ids, ["primary-", "new-sess", "worktree"])
        self.assertIn("| 800 |", next(line for line in second if "| primary- |" in line))
        self.assertIn("1 rows kept from earlier writes", "\n".join(second))

    def test_parse_refusals_leave_the_file_unchanged(self) -> None:
        for corruption in ("| extra", ""):
            with self.subTest(corruption=corruption):
                self.record.unlink(missing_ok=True)
                lines = self.write().splitlines()
                index = next(i for i, line in enumerate(lines) if "| primary- |" in line)
                lines[index] = (lines[index] + corruption) if corruption else lines[index].replace(" 600 ", " x ")
                self.record.write_text("\n".join(lines) + "\n", encoding="utf-8")
                before = self.record.read_bytes()
                code, stdout, stderr = run_cli(["--write", str(self.record)], CHECKOUT, self.fixture.projects)
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("Restore .scratch/CYCLES.md from git", stderr)
                self.assertEqual(before, self.record.read_bytes())

    def test_short_id_collision_refuses_without_writing(self) -> None:
        for stem in ("abcdef12-one", "abcdef12-two"):
            write_jsonl(self.fixture.primary / f"{stem}.jsonl", [assistant(stem, "2026-01-03T00:00:00Z", (1, 1, 1, 1))])
        code, stdout, stderr = run_cli(["--write", str(self.record)], CHECKOUT, self.fixture.projects)
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("share the short id abcdef12", stderr)
        self.assertFalse(self.record.exists())

    def test_default_path_is_the_checkout_record(self) -> None:
        checkout = Path(self.temporary.name) / "checkout"
        (checkout / ".git").mkdir(parents=True)
        projects = Path(self.temporary.name) / "store"
        write_jsonl(projects / cycles.path_slug(checkout) / "s.jsonl", [assistant("d", "2026-01-01T00:00:00Z", (2, 3, 4, 5))])
        code, stdout, stderr = run_cli(["--write"], checkout, projects)
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue((checkout / ".scratch" / "CYCLES.md").is_file())


class ImportBoundary(TestCase):
    def test_cycles_imports_only_standard_library_modules(self) -> None:
        path = ROOT / "tools" / "cycles.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                self.assertIn(name.partition(".")[0], sys.stdlib_module_names, f"{path}:{node.lineno}")
