"""Read the harness's transcripts and write the cycles record: one row per session.

The tool emits identifiers and computed figures only; no prompt text reaches
the record.  ``--write`` merges the present sessions into ``.scratch/CYCLES.md``
by session id, and ``--latest`` prints one session's line for the seam count.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

DEFAULT_PROJECTS: Final[Path] = Path.home() / ".claude" / "projects"
WORKTREE_MARKER: Final[str] = "--claude-worktrees-"
# The two lines CLAUDE.md's Ticket sizing draws: the early warning and the seam.
EARLY_WARNING: Final[int] = 800_000
SEAM_TURNS: Final[int] = 150
DEFAULT_RECORD: Final[Path] = Path(".scratch") / "CYCLES.md"
TRIP_SKILLS: Final[frozenset[str]] = frozenset({"TRIP-1-plan", "TRIP-2-implement", "TRIP-3-release"})
USAGE_FIELDS: Final[tuple[str, ...]] = (
    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
)
COMMAND_PATTERN: Final[re.Pattern[str]] = re.compile(r"<command-name>\s*/?([\w-]+)</command-name>")
TICKET_PATTERN: Final[re.Pattern[str]] = re.compile(r"\.scratch/([\w-]+)/issues/(\d+)-[\w-]+\.md")
# A plan is named by its version when the file name carries one, else by its slug.
PLAN_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<![\w.-])(?:F_(\d+\.\d+\.\d+)_)?([\w-]+)\.plan\.md")

RECORD_TITLE: Final[str] = (
    "# The cycles record: one row per session of this checkout, from the harness's transcripts"
)
BANDS_HEADING: Final[str] = "## Bands"
SESSIONS_HEADING: Final[str] = "## Sessions"
BAND_COLUMNS: Final[tuple[str, ...]] = (
    "Band", "sessions", "median peak", "max peak", f"above {EARLY_WARNING // 1000}k",
    "median turns", "max turns", f"above {SEAM_TURNS} turns", "compactions",
)
SESSION_COLUMNS: Final[tuple[str, ...]] = (
    "started (UTC)", "session", "where", "skill", "model", "effort", "tickets", "plans",
    "turns", "peak context", "compactions",
)
_RECORD_FIX: Final[str] = "Restore .scratch/CYCLES.md from git, or remove it to start the record again."


class CyclesError(Exception):
    """A refusal with the problem and the action that fixes it."""

    def __init__(self, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.fix = fix


class _MalformedTranscript(Exception):
    """A transcript that cannot produce a row."""


@dataclass(frozen=True, slots=True)
class Session:
    """One row: identifiers and figures for one session."""

    id: str
    started: str
    where: str
    skill: str
    models: tuple[str, ...]
    efforts: tuple[str, ...]
    tickets: tuple[str, ...]
    plans: tuple[str, ...]
    turns: int
    peak: int
    compactions: int
    last: str = ""

    @property
    def short_id(self) -> str:
        return self.id[:8]

    @property
    def bands(self) -> tuple[str, ...]:
        """The efforts a cycle session counts in: its tickets' directories, else unattributed."""

        if self.skill not in TRIP_SKILLS:
            return ()
        return tuple(sorted({ticket.split("/", 1)[0] for ticket in self.tickets})) or ("unattributed",)


@dataclass(frozen=True, slots=True)
class Report:
    """The counts of one store read and one write."""

    transcripts_read: int = 0
    without_usage: int = 0
    malformed_paths: tuple[Path, ...] = ()
    rows_rewritten: int = 0
    rows_kept: int = 0


def path_slug(path: Path) -> str:
    """Return the harness's directory name for a checkout path."""

    return "".join(character if character.isalnum() else "-" for character in str(path))


def primary_checkout(checkout: Path) -> Path:
    """Resolve the primary checkout a worktree's ``.git`` file names."""

    root = checkout.resolve()
    git_path = root / ".git"
    if not git_path.is_file():
        return root
    try:
        line = git_path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        line = ""
    gitdir = Path(line.removeprefix("gitdir:").strip())
    text = (gitdir if gitdir.is_absolute() else root / gitdir).resolve().as_posix()
    position = text.find("/.git/")
    if not line.startswith("gitdir:") or position < 0:
        raise CyclesError(
            f"the checkout's .git file at {git_path} names no primary checkout",
            "Restore the worktree's metadata or run from the primary, then retry.",
        )
    return Path(text[:position])


def family_directories(primary: Path, store: Path) -> tuple[tuple[Path, str], ...]:
    """Return the store's directories for the primary and its worktrees, labelled."""

    name = path_slug(primary)
    found: list[tuple[Path, str]] = []
    for directory in sorted(store.iterdir(), key=lambda item: item.name) if store.is_dir() else ():
        if directory.is_dir() and directory.name == name:
            found.append((directory, "(primary)"))
        elif directory.is_dir() and directory.name.startswith(name + WORKTREE_MARKER):
            found.append((directory, "worktree " + directory.name[len(name + WORKTREE_MARKER) :]))
    return tuple(found)


def _where(cwds: Iterable[str], primary: Path, fallback: str) -> str:
    """Name the first worktree a session entered, else the primary."""

    seen_primary = False
    for cwd in cwds:
        try:
            parts = Path(cwd).resolve().relative_to(primary).parts
        except ValueError:
            continue
        if len(parts) >= 3 and parts[:2] == (".claude", "worktrees"):
            return f"worktree {parts[2]}"
        seen_primary = True
    return "(primary)" if seen_primary else fallback


def _text(message: dict[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else ()
    return " ".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))


def _skill_tool_uses(message: dict[str, object]) -> Iterable[str]:
    content = message.get("content")
    for block in content if isinstance(content, list) else ():
        if isinstance(block, dict) and block.get("name") == "Skill":
            tool_input = block.get("input")
            skill = tool_input.get("skill") if isinstance(tool_input, dict) else None
            if isinstance(skill, str):
                yield skill.rsplit(":", 1)[-1]


def _usage(value: object) -> tuple[int, ...]:
    if not isinstance(value, dict) or any(
        not isinstance(value.get(name), int) or isinstance(value.get(name), bool) for name in USAGE_FIELDS
    ):
        raise _MalformedTranscript("assistant usage does not have four integer fields")
    return tuple(value[name] for name in USAGE_FIELDS)


def _add(values: list[str], value: object) -> None:
    if isinstance(value, str) and value and not value.startswith("<") and value not in values:
        values.append(value)


def read_transcript(path: Path, primary: Path, fallback_where: str = "") -> Session | None:
    """Read one transcript; no row when it carries no usage."""

    first = last = skill = trip_skill = ""
    cwds: dict[str, None] = {}
    models: list[str] = []
    efforts: list[str] = []
    tickets: set[str] = set()
    plans: set[str] = set()
    responses: dict[str, tuple[int, ...]] = {}
    peak = compactions = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            timestamp = entry.get("timestamp")
            if isinstance(timestamp, str) and timestamp:
                first, last = first or timestamp, timestamp
            if isinstance(entry.get("cwd"), str):
                cwds.setdefault(entry["cwd"])
            compactions += entry.get("subtype") == "compact_boundary"
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            if entry.get("type") == "user":
                text = _text(message)
                commands = COMMAND_PATTERN.findall(text)
                skill = skill or (commands[0] if commands else "")
                trip_skill = trip_skill or next((c for c in commands if c in TRIP_SKILLS), "")
                tickets.update(f"{effort}/{number}" for effort, number in TICKET_PATTERN.findall(text))
                plans.update(version or slug for version, slug in PLAN_PATTERN.findall(text))
            if entry.get("type") != "assistant":
                continue
            trip_skill = trip_skill or next((s for s in _skill_tool_uses(message) if s in TRIP_SKILLS), "")
            if "usage" not in message:
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str) or not message_id:
                raise _MalformedTranscript("assistant usage has no message id")
            usage = _usage(message["usage"])
            if responses.setdefault(message_id, usage) != usage:
                raise _MalformedTranscript(f"usage differs for repeated message id {message_id}")
            peak = max(peak, sum(usage[:3]))
            _add(models, message.get("model"))
            _add(efforts, entry.get("effort"))
    if not responses:
        return None
    return Session(
        path.stem, first[:16].replace("T", " "), _where(cwds, primary, fallback_where), trip_skill or skill,
        tuple(models), tuple(efforts), tuple(sorted(tickets)), tuple(sorted(plans)),
        len(responses), peak, compactions, last,
    )


def read_family(checkout: Path, store: Path) -> tuple[tuple[Session, ...], Report]:
    """Read every transcript of the checkout's family in one store."""

    primary = primary_checkout(checkout)
    directories = family_directories(primary, store)
    if not directories:
        raise CyclesError(
            f"no harness directory for the checkout {primary} in the store {store}",
            "Run from the machine whose home holds the harness's transcripts, "
            "or pass --projects <dir> naming its store, then retry.",
        )
    read = without_usage = 0
    malformed: list[Path] = []
    by_id: dict[str, Session] = {}
    for directory, fallback_where in directories:
        for path in sorted(directory.glob("*.jsonl"), key=lambda item: item.name):
            read += 1
            try:
                session = read_transcript(path, primary, fallback_where)
            except (_MalformedTranscript, OSError):
                malformed.append(path)
            else:
                without_usage += session is None
                if session is not None and (session.id not in by_id or by_id[session.id].last < session.last):
                    by_id[session.id] = session
    by_short: dict[str, Session] = {}
    for session in by_id.values():
        other = by_short.setdefault(session.short_id, session)
        if other is not session:
            raise CyclesError(
                f"present sessions {other.id} and {session.id} share the short id {session.short_id}",
                "Widen the record's key in tools/cycles.py, or pass --projects <dir> "
                "naming a copy of the store with one of the two omitted.",
            )
    sessions = tuple(sorted(by_id.values(), key=lambda item: (item.started, item.id)))
    return sessions, Report(read, without_usage, tuple(malformed))


def _number(value: int | None) -> str:
    return "" if value is None else f"{value:,}"


def _median(values: Sequence[int]) -> str:
    return _number(int(statistics.median(values))) if values else ""


def _band(name: str, sessions: Sequence[Session]) -> tuple[str, ...]:
    peaks = sorted(session.peak for session in sessions)
    turns = sorted(session.turns for session in sessions)
    return (
        name, str(len(sessions)), _median(peaks), _number(max(peaks, default=None)),
        str(sum(peak > EARLY_WARNING for peak in peaks)), _median(turns), _number(max(turns, default=None)),
        str(sum(count > SEAM_TURNS for count in turns)), str(sum(s.compactions for s in sessions)),
    )


def band_rows(sessions: Sequence[Session]) -> tuple[tuple[str, ...], ...]:
    """The band table's cells: the sessions per effort, the unattributed cycles, the triages, and all."""

    efforts: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        for effort in session.bands:
            efforts[effort].append(session)
    names = sorted(efforts, key=lambda name: (name == "unattributed", name))
    rows = [_band(f"{name} cycle sessions", efforts[name]) for name in names]
    rows.append(_band("triage sessions", [s for s in sessions if s.skill == "triage"]))
    return (*rows, _band("every session", sessions))


def _row(values: Iterable[str]) -> str:
    return "| " + " | ".join(values) + " |"


def render_tables(sessions: Sequence[Session]) -> str:
    """Render the band table and the sessions table."""

    lines = [BANDS_HEADING, "", _row(BAND_COLUMNS), _row("---" for _ in BAND_COLUMNS)]
    lines.extend(_row(cells) for cells in band_rows(sessions))
    lines += ["", SESSIONS_HEADING, "", _row(SESSION_COLUMNS), _row("---" for _ in SESSION_COLUMNS)]
    for s in sessions:
        lines.append(_row((s.started, s.short_id, s.where, s.skill, ", ".join(s.models), ", ".join(s.efforts),
                           ", ".join(s.tickets), ", ".join(s.plans), str(s.turns), _number(s.peak),
                           str(s.compactions))))
    return "\n".join(lines) + "\n"


def render_record(sessions: Sequence[Session], report: Report) -> str:
    """Render the committed record: the preamble, the counts, and the two tables."""

    preamble = (
        "One row per session of this checkout and its worktrees, read from the harness's transcripts "
        "in `~/.claude/projects` (`--projects` names another store): when it started, its short id, "
        "where it ran, the first TRIP skill it invoked (else the first command), the models and efforts "
        "its responses carried, the tickets and plans its prompts named, its turns (one per response id), "
        "its peak context (the largest prompt one response sent: the input, cache-creation, and "
        "cache-read tokens together), and its compactions. A cycle session invoked a TRIP skill; the "
        "band table counts it in each effort its tickets name, unattributed when they name none. Ids "
        "and figures only, never a prompt's text. Written on demand by `python3 -m tools.cycles --write`, "
        "which merges by session id and keeps a row whose transcript is gone; the record to `v0.1.61` "
        "in its former shape is `.scratch/workflow/assets/42-cycles-record-to-v0.1.61.md`."
    )
    with_usage = report.transcripts_read - report.without_usage - len(report.malformed_paths)
    present = (
        f"Present: {with_usage} transcripts with usage; {report.without_usage} without; "
        f"{len(report.malformed_paths)} malformed; {report.rows_kept} rows kept from earlier writes "
        "whose transcripts are absent."
    )
    return "\n\n".join((RECORD_TITLE, preamble, present, render_tables(sessions)))


def _cells(line: str) -> list[str]:
    if not (line.strip().startswith("|") and line.strip().endswith("|")):
        raise ValueError("not a table row")
    return [cell.strip() for cell in line.strip()[1:-1].split("|")]


def _count(value: str) -> int:
    if not re.fullmatch(r"\d[\d,]*", value):
        raise ValueError(f"{value!r} is not a number")
    return int(value.replace(",", ""))


def _list(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_record(text: str) -> tuple[Session, ...]:
    """Parse the kept rows of a record; a refusal names the fix and writes nothing."""

    lines = text.splitlines()
    try:
        rows = [line for line in lines[lines.index(SESSIONS_HEADING) + 1 :] if line.strip()]
        if _cells(rows[0]) != list(SESSION_COLUMNS) or _cells(rows[1]) != ["---"] * len(SESSION_COLUMNS):
            raise ValueError("the Sessions table has no header and separator in the column set")
        sessions: dict[str, Session] = {}
        for row in rows[2:]:
            c = _cells(row)
            if len(c) != len(SESSION_COLUMNS):
                raise ValueError("a Sessions row has the wrong cell count")
            if not c[1] or c[1] in sessions:
                raise ValueError("a Sessions row has a missing or repeated session id")
            sessions[c[1]] = Session(
                c[1], c[0], c[2], c[3], _list(c[4]), _list(c[5]), _list(c[6]), _list(c[7]),
                _count(c[8]), _count(c[9]), _count(c[10]),
            )
        return tuple(sessions.values())
    except (IndexError, ValueError) as error:
        raise CyclesError(f"the cycles record cannot be parsed: {error}", _RECORD_FIX) from error


def merge_rows(
    kept: Sequence[Session], present: Sequence[Session], report: Report
) -> tuple[tuple[Session, ...], Report]:
    """Replace kept rows by present rows and keep the rows whose transcripts are gone."""

    present_ids = {session.short_id for session in present}
    merged = [session for session in kept if session.short_id not in present_ids] + list(present)
    merged.sort(key=lambda session: (session.started, session.short_id))
    return tuple(merged), replace(report, rows_rewritten=len(present), rows_kept=len(merged) - len(present))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tools.cycles")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--write", nargs="?", const=DEFAULT_RECORD, type=Path, metavar="PATH")
    output.add_argument("--latest", action="store_true")
    parser.add_argument("--session", metavar="PREFIX")
    parser.add_argument("--checkout", type=Path, metavar="DIR")
    parser.add_argument("--projects", type=Path, metavar="DIR")
    return parser


def _write(record_path: Path, sessions: Sequence[Session], report: Report) -> tuple[int, Report]:
    kept: tuple[Session, ...] = ()
    if record_path.exists():
        try:
            kept = parse_record(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as error:
            raise CyclesError(f"the cycles record cannot be read: {error}", _RECORD_FIX) from error
    merged, report = merge_rows(kept, sessions, report)
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(render_record(merged, report), encoding="utf-8")
    except OSError as error:
        raise CyclesError(
            f"cannot write the cycles record at {record_path}: {error}",
            "Choose a writable path with --write <path>, then retry.",
        ) from error
    return len(merged), report


def main(argv: Sequence[str] | None = None, *, checkout: Path | None = None, projects: Path | None = None) -> int:
    """Read the checkout's family and print its tables, one session's line, or write the record."""

    options = _parser().parse_args(argv)
    selected = (options.checkout or checkout or Path(__file__).resolve().parents[1]).resolve()
    store = (options.projects or projects or DEFAULT_PROJECTS).expanduser().resolve()
    try:
        if options.session is not None and not options.latest:
            raise CyclesError(
                "--session can only be used with --latest",
                "Use --session <prefix> together with --latest, or omit --session.",
            )
        sessions, report = read_family(selected, store)
        if options.latest:
            if not sessions:
                raise CyclesError(
                    "no present session has usage in the harness store",
                    "Run the tool without --latest and read its report, then retry.",
                )
            matches = [s for s in sessions if options.session is None or s.id.startswith(options.session)]
            if options.session is not None and len(matches) != 1:
                raise CyclesError(
                    f"{'no' if not matches else 'more than one'} present session id starts with {options.session}",
                    "Use a --session prefix selecting exactly one session, or omit it for the latest.",
                )
            latest = max(matches, key=lambda session: (session.last, session.id))
            sys.stdout.write(
                f"latest session {latest.short_id} ({latest.where}, {latest.skill or '-'}): "
                f"{latest.turns} turns of {SEAM_TURNS}, peak {_number(latest.peak)} of "
                f"{_number(EARLY_WARNING)}, model {', '.join(latest.models) or '-'}, "
                f"effort {', '.join(latest.efforts) or '-'}\n"
            )
            return 0
        lines = [
            f"transcripts read: {report.transcripts_read}",
            f"transcripts without usage: {report.without_usage}",
            f"malformed transcripts: {len(report.malformed_paths)}",
            *(f"malformed path: {path}" for path in report.malformed_paths),
        ]
        if options.write is None:
            sys.stdout.write(render_tables(sessions) + "\n## Report\n\n" + "\n".join(lines) + "\n")
            return 0
        record_path = (selected / options.write.expanduser()).resolve()
        count, report = _write(record_path, sessions, report)
        lines.append(f"wrote {record_path}: {count} rows ({report.rows_rewritten} rewritten, {report.rows_kept} kept).")
        sys.stdout.write("\n".join(lines) + "\n")
        return 0
    except CyclesError as error:
        print(f"python3 -m tools.cycles: {error.problem} Fix: {error.fix}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
