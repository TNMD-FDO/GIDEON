"""Read and render the local Markdown issue tracker; ``--check`` writes no outputs."""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

# The threshold belongs to docs/agents/issue-tracker.md's board convention.
TRIAGE_THRESHOLD_DAYS: Final[int] = 7
# The standing effort name belongs to docs/agents/issue-tracker.md's standing convention.
STANDING_EFFORT: Final[str] = "standing"
DEFAULT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
BOARD_PATH: Final[Path] = Path(".scratch") / "BOARD.md"
PAGE_PATH: Final[Path] = Path(".scratch") / "BOARD.html"
CRITICAL_MARK: Final[str] = "* "
CRITICAL_MARK_NOTE: Final[str] = f"Rows marked {CRITICAL_MARK.strip()} are on the critical path."
TRIAGE_TITLE: Final[str] = f"Triage owed (past {TRIAGE_THRESHOLD_DAYS} days)"
NO_RELEASE_TICKET: Final[str] = "No open release ticket carries a Tag: line."
EMPTY: Final[str] = "none"
EXTERNAL: Final[str] = "external"
COLUMNS: Final[tuple[str, ...]] = ("number", "title", "status", "blockers", "age (days)")
FRONTIER_COLUMNS: Final[tuple[str, ...]] = ("number", "title", "age (days)")
# Colour tokens defined once and redefined for the dark scheme; status is text, never colour alone.
HTML_STYLE: Final[str] = """\
:root {
  --page: #f7f7f2;
  --text: #202124;
  --muted: #5f6368;
  --border: #c8c8c0;
  --accent: #315a8a;
  --header: #e6ebf2;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #202124;
    --text: #f1f3f4;
    --muted: #bdc1c6;
    --border: #5f6368;
    --accent: #9cc7ff;
    --header: #2b3340;
  }
}
body { background: var(--page); color: var(--text); font: 16px sans-serif; line-height: 1.45; margin: 2rem auto; max-width: 76rem; padding: 0 1rem; }
h1, h2, h3 { color: var(--accent); }
p[data-kind="metadata"] { color: var(--muted); }
table { border-collapse: collapse; margin: 0 0 1.5rem; width: 100%; }
th, td { border: 1px solid var(--border); padding: .45rem .6rem; text-align: left; vertical-align: top; }
th { background: var(--header); }
td[data-status] { font-weight: 600; white-space: nowrap; }
a { color: var(--accent); }
del { color: var(--muted); }"""
TICKET_NAME: Final[re.Pattern[str]] = re.compile(r"^(?P<number>\d{2})-(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*)\.md$")
HEADING: Final[re.Pattern[str]] = re.compile(r"^#\s+(?P<number>\d{2})(?::|\s+—)\s*(?P<title>.*)$")
STATUS_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:\*\*)?Status:\*{0,2}\s*(?P<value>.*?)\s*$")
BLOCKER_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:\*\*)?Blocked by:\*{0,2}\s*(?P<value>.*?)\s*$")
TAG_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:\*\*)?Tag:\*{0,2}\s*(?P<value>.*?)\s*$")
DATE_LINE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BARE_NUMBER: Final[re.Pattern[str]] = re.compile(r"(?<![\d.\-])(?P<number>\d{2})(?![\d.\-])")
RANGE: Final[re.Pattern[str]] = re.compile(r"(?<![\d.\-])(?P<first>\d{2})–(?P<last>\d{2})(?![\d.\-])")
EFFORT_REFS: Final[re.Pattern[str]] = re.compile(
    r"(?P<effort>[A-Za-z0-9_-]+)\s+tickets?\s+"
    r"(?P<numbers>\d{2}(?:–\d{2})?(?:(?:(?:\s*,\s*(?:and\s+)?)|(?:\s+and\s+))\d{2}(?:–\d{2})?)*)"
)
LOCAL_REF: Final[re.Pattern[str]] = re.compile(r"\bticket\s+(?P<number>\d{2})\b")
NONE_WORD: Final[re.Pattern[str]] = re.compile(r"^(?:none|nothing)\b", re.IGNORECASE)
FIRED_WORD: Final[re.Pattern[str]] = re.compile(r"^fired\s*\.?$", re.IGNORECASE)
STATUS_TOKEN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION: Final[re.Pattern[str]] = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


class TrackerError(Exception):
    """A refusal with the problem and the action that fixes it."""

    def __init__(self, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.fix = fix


@dataclass(frozen=True, slots=True)
class Reference:
    """One ticket named by a blocker clause."""

    effort: str
    number: int


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """The open and closing statuses read from the tracker document."""

    open_statuses: tuple[str, ...]
    closed_statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitFacts:
    """Dates and working-tree paths obtained from one git snapshot."""

    opened_dates: tuple[tuple[Path, date], ...]
    uncommitted_paths: frozenset[Path]

    def opened(self, path: Path) -> date | None:
        """Return the first-commit date for a relative path, if known."""

        return dict(self.opened_dates).get(path)


@dataclass(frozen=True, slots=True)
class Ticket:
    """The public, body-free model of one issue file."""

    effort: str
    number: int
    path: Path
    title: str
    status: str
    blocker_line: str
    references: tuple[Reference, ...]
    external_blocker: bool
    fired: bool
    tag: str | None
    opened_date: date | None
    uncommitted: bool
    status_line: int | None
    blocker_line_number: int | None


@dataclass(frozen=True, slots=True)
class Finding:
    """One actionable lint or shape finding."""

    path: Path
    line: int
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class Board:
    """All sections rendered by the board's Markdown and HTML renderers."""

    today: date
    vocabulary: Vocabulary
    tickets: tuple[Ticket, ...]
    total_tickets: int
    open_tickets: int
    efforts_read: int
    efforts_open: int
    git_facts_present: bool
    findings: tuple[Finding, ...]
    next_tag: str | None
    release_ticket: Ticket | None
    release_tickets: tuple[Ticket, ...]
    critical_path: tuple[Ticket, ...]
    frontier: tuple[Ticket, ...]
    decisions_owed: tuple[Ticket, ...]
    triage_owed: tuple[Ticket, ...]
    listing: tuple[tuple[str, tuple[Ticket, ...]], ...]
    standing: tuple[Ticket, ...]
    standing_open: int


def _without_parentheses(value: str) -> str:
    """Remove balanced parenthesised spans, including nested spans."""

    result: list[str] = []
    depth = 0
    for character in value:
        if character == "(":
            depth += 1
        elif character == ")" and depth:
            depth -= 1
        elif depth == 0:
            result.append(character)
    return "".join(result)


def _first_sentence(value: str) -> str:
    match = re.search(r"\.(?=\s+[A-Z])", value)
    return value[: match.end()] if match else value


def _number_list(value: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for item in re.finditer(r"\d{2}(?:–\d{2})?", value):
        if "–" in item.group():
            first, last = (int(part) for part in item.group().split("–"))
            numbers.extend(range(first, last + 1))
        else:
            numbers.append(int(item.group()))
    return tuple(numbers)


def _add_reference(references: list[Reference], reference: Reference) -> None:
    if reference not in references:
        references.append(reference)


def parse_blockers(
    value: str | None, effort: str
) -> tuple[tuple[Reference, ...], bool, bool]:
    """Parse blocker references, external blockers, and the fired clause."""

    if value is None:
        return (), False, False
    sentence = _first_sentence(_without_parentheses(value)).strip()
    if not sentence:
        return (), False, False
    if NONE_WORD.match(sentence) or sentence.startswith(("—", "-")):
        return (), False, False

    references: list[Reference] = []
    external = False
    fired = False
    for clause in sentence.split(";"):
        if FIRED_WORD.fullmatch(clause.strip()):
            fired = True
            continue
        clause_references: list[Reference] = []
        for match in EFFORT_REFS.finditer(clause):
            for number in _number_list(match.group("numbers")):
                _add_reference(clause_references, Reference(match.group("effort"), number))
        scrubbed = EFFORT_REFS.sub(" ", clause)
        for match in RANGE.finditer(scrubbed):
            for number in range(int(match.group("first")), int(match.group("last")) + 1):
                _add_reference(clause_references, Reference(effort, number))
        scrubbed = RANGE.sub(" ", scrubbed)
        for match in LOCAL_REF.finditer(scrubbed):
            _add_reference(clause_references, Reference(effort, int(match.group("number"))))
        scrubbed = LOCAL_REF.sub(" ", scrubbed)
        for match in BARE_NUMBER.finditer(scrubbed):
            _add_reference(clause_references, Reference(effort, int(match.group("number"))))
        if not clause_references:
            external = True
        for reference in clause_references:
            _add_reference(references, reference)
    return tuple(references), external, fired


def read_vocabulary(root: Path) -> Vocabulary:
    """Read and validate the one machine-readable status line."""

    path = root / "docs" / "agents" / "issue-tracker.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise TrackerError(
            f"cannot read the vocabulary document at {path}: {error}",
            "Restore docs/agents/issue-tracker.md with its machine-readable Statuses: line, then retry.",
        ) from error
    matches = [line.strip() for line in lines if line.strip().startswith("Statuses:")]
    if len(matches) != 1:
        raise TrackerError(
            f"the vocabulary document at {path} has no single machine-readable Statuses: line",
            "Add exactly one `Statuses: open, statuses; closed: closing, statuses` line to docs/agents/issue-tracker.md, then retry.",
        )
    match = re.fullmatch(r"Statuses:\s*([^;]+);\s*closed:\s*(.+)", matches[0])
    if match is None:
        raise TrackerError(
            f"the vocabulary line in {path} is malformed",
            "Use `Statuses: open, statuses; closed: closing, statuses` on one line in docs/agents/issue-tracker.md, then retry.",
        )
    open_statuses = tuple(item.strip() for item in match.group(1).split(","))
    closed_statuses = tuple(item.strip() for item in match.group(2).split(","))
    all_statuses = open_statuses + closed_statuses
    if (
        not open_statuses
        or not closed_statuses
        or any(not STATUS_TOKEN.fullmatch(item) for item in all_statuses)
        or len(set(all_statuses)) != len(all_statuses)
    ):
        raise TrackerError(
            f"the vocabulary line in {path} is malformed",
            "List each non-empty lowercase status once as `Statuses: open, statuses; closed: closing, statuses`, then retry.",
        )
    return Vocabulary(open_statuses, closed_statuses)


def _parse_git_log(output: str) -> dict[Path, date]:
    opened: dict[Path, date] = {}
    current_date: date | None = None
    records: list[tuple[date, list[str]]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if DATE_LINE.fullmatch(line):
            current_date = date.fromisoformat(line)
            continue
        fields = raw_line.split("\t")
        if current_date is None or not fields:
            continue
        records.append((current_date, fields))
    for current_date, fields in reversed(records):
        status = fields[0]
        if status.startswith("R") and len(fields) >= 3:
            old_path = Path(fields[-2])
            new_path = Path(fields[-1])
            if old_path in opened:
                opened[new_path] = opened.pop(old_path)
            else:
                opened[new_path] = current_date
        elif status.startswith("A") and len(fields) >= 2:
            opened.setdefault(Path(fields[-1]), current_date)
    return opened


def _status_paths(output: str) -> frozenset[Path]:
    paths: set[Path] = set()
    for line in output.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[1]
        path = Path(value.strip().strip('"'))
        if path.parts and path.parts[0] == ".scratch":
            paths.add(path)
    return frozenset(paths)


def read_git_facts(root: Path) -> GitFacts | None:
    """Read git dates and worktree state, or return absent facts on any failure."""

    try:
        log_result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "--name-status",
                "--find-renames",
                "--format=%ad",
                "--date=short",
                "--",
                ".scratch",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        status_result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".scratch",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if log_result.returncode != 0 or status_result.returncode != 0:
        return None
    try:
        dates = _parse_git_log(log_result.stdout)
    except ValueError:
        return None
    relative_uncommitted = _status_paths(status_result.stdout)
    return GitFacts(tuple(sorted(dates.items())), relative_uncommitted)


def _find_efforts(root: Path) -> tuple[Path, ...]:
    scratch = root / ".scratch"
    if not scratch.is_dir():
        return ()
    return tuple(sorted(path for path in scratch.iterdir() if (path / "issues").is_dir()))


def _header_value(lines: Sequence[str], pattern: re.Pattern[str]) -> tuple[str | None, int | None]:
    for index, line in enumerate(lines, start=1):
        match = pattern.match(line)
        if match is not None:
            return match.group("value").strip(), index
    return None, None


def _read_ticket(root: Path, effort_path: Path, path: Path, number: int, facts: GitFacts | None) -> Ticket:
    lines = path.read_text(encoding="utf-8").splitlines()
    title = "untitled"
    for line in lines:
        match = HEADING.match(line)
        if match is not None:
            title = match.group("title").strip() or "untitled"
            break
    status, status_line = _header_value(lines, STATUS_LINE)
    blocker_line, blocker_line_number = _header_value(lines, BLOCKER_LINE)
    tag, _ = _header_value(lines, TAG_LINE)
    effort = effort_path.name
    references, external, fired = parse_blockers(blocker_line, effort)
    relative = path.relative_to(root)
    return Ticket(
        effort,
        number,
        relative,
        title,
        status if status else "missing",
        blocker_line if blocker_line is not None else "none",
        references,
        external,
        fired,
        tag or None,
        facts.opened(relative) if facts is not None else None,
        facts is not None and relative in facts.uncommitted_paths,
        status_line,
        blocker_line_number,
    )


def read_tickets(root: Path, facts: GitFacts | None) -> tuple[tuple[Ticket, ...], tuple[Finding, ...], tuple[Path, ...]]:
    """Read each shaped ticket once and return tickets, shape findings, and efforts."""

    efforts = _find_efforts(root)
    tickets: list[Ticket] = []
    findings: list[Finding] = []
    for effort_path in efforts:
        issues = effort_path / "issues"
        for path in sorted(issues.iterdir()):
            if not path.is_file():
                continue
            name_match = TICKET_NAME.fullmatch(path.name)
            if name_match is None:
                findings.append(
                    Finding(
                        path.relative_to(root),
                        1,
                        "issue file has a filename outside NN-<slug>.md",
                        "Rename it to NN-<slug>.md with a two-digit ticket number and a slug",
                    )
                )
                continue
            try:
                tickets.append(
                    _read_ticket(root, effort_path, path, int(name_match.group("number")), facts)
                )
            except (OSError, UnicodeError) as error:
                findings.append(
                    Finding(
                        path.relative_to(root),
                        1,
                        f"issue file cannot be read: {error}",
                        "Restore a readable UTF-8 issue file, then retry",
                    )
                )
    return tuple(tickets), tuple(findings), efforts


def _ticket_key(ticket: Ticket) -> tuple[str, int]:
    return ticket.effort, ticket.number


def _reference_key(reference: Reference) -> tuple[str, int]:
    return reference.effort, reference.number


def _open(ticket: Ticket, vocabulary: Vocabulary) -> bool:
    return ticket.status not in vocabulary.closed_statuses


def _reference_map(tickets: Iterable[Ticket]) -> dict[tuple[str, int], Ticket]:
    references: dict[tuple[str, int], Ticket] = {}
    for ticket in tickets:
        references.setdefault(_ticket_key(ticket), ticket)
    return references


def _offered_now(ticket: Ticket) -> bool:
    """Return whether a ticket belongs in a section of work owed now."""

    return ticket.effort != STANDING_EFFORT or (ticket.fired and not ticket.external_blocker)


def _lint(
    tickets: Sequence[Ticket], vocabulary: Vocabulary, facts: GitFacts | None, shape_findings: Sequence[Finding]
) -> tuple[Finding, ...]:
    by_reference = _reference_map(tickets)
    findings = list(shape_findings)
    seen_numbers: dict[tuple[str, int], Ticket] = {}
    for ticket in tickets:
        key = _ticket_key(ticket)
        earlier = seen_numbers.setdefault(key, ticket)
        if earlier is not ticket:
            findings.append(
                Finding(
                    ticket.path,
                    1,
                    f"ticket number {ticket.number:02d} duplicates {earlier.path}",
                    "Renumber one of the two files to the next free number in the effort and record its former identity in the first line of its body",
                )
            )
        if ticket.status not in vocabulary.open_statuses + vocabulary.closed_statuses:
            findings.append(
                Finding(
                    ticket.path,
                    ticket.status_line or 0,
                    f"status {ticket.status!r} is outside the vocabulary",
                    "Add `Status: <canonical status>` using a value from docs/agents/issue-tracker.md",
                )
            )
        if not _open(ticket, vocabulary):
            continue
        missing = [
            reference for reference in ticket.references if _reference_key(reference) not in by_reference
        ]
        if missing:
            names = ", ".join(f"{item.effort} ticket {item.number:02d}" for item in missing)
            findings.append(
                Finding(
                    ticket.path,
                    ticket.blocker_line_number or 0,
                    f"open blocker reference does not name an existing ticket: {names}",
                    "Correct the Blocked by line to name an existing effort and ticket number",
                )
            )
        closed = [
            reference
            for reference in ticket.references
            if _reference_key(reference) in by_reference
            and not _open(by_reference[_reference_key(reference)], vocabulary)
        ]
        if closed:
            names = ", ".join(f"{item.effort} ticket {item.number:02d}" for item in closed)
            findings.append(
                Finding(
                    ticket.path,
                    ticket.blocker_line_number or 0,
                    f"open ticket is blocked by resolved {names}",
                    "Name the resolved blocker in parentheses with its tag, or rewrite the line as `none`",
                )
            )
        if ticket.effort == STANDING_EFFORT:
            if not ticket.fired and not ticket.external_blocker:
                findings.append(
                    Finding(
                        ticket.path,
                        ticket.blocker_line_number or 0,
                        "standing ticket names no event in its Blocked by line",
                        "Name the event no session can cause as a clause of the Blocked by line's first sentence, write fired when it has fired, or move the ticket back to its effort",
                    )
                )
            elif ticket.fired and ticket.external_blocker:
                findings.append(
                    Finding(
                        ticket.path,
                        ticket.blocker_line_number or 0,
                        "standing ticket is both fired and waiting",
                        "Keep fired and move the event into the parenthesis, or drop fired while the event stands",
                    )
                )
            elif ticket.status == "claimed" and not ticket.fired:
                findings.append(
                    Finding(
                        ticket.path,
                        ticket.blocker_line_number or 0,
                        "claimed standing ticket still names its event as a blocker",
                        "Write fired with the event in the parenthesis, or unclaim the ticket",
                    )
                )
        if facts is not None and ticket.status == "claimed" and ticket.uncommitted:
            findings.append(
                Finding(
                    ticket.path,
                    ticket.status_line or 0,
                    "claimed ticket is modified or untracked",
                    "Commit the claim with the cycle's release or revert the claim",
                )
            )
    findings.extend(_cycle_findings(tickets, vocabulary))
    return tuple(findings)


def _cycle_findings(tickets: Sequence[Ticket], vocabulary: Vocabulary) -> tuple[Finding, ...]:
    by_reference = _reference_map(tickets)
    graph = {
        _ticket_key(ticket): tuple(
            _ticket_key(by_reference[_reference_key(reference)])
            for reference in ticket.references
            if _reference_key(reference) in by_reference
            and _open(by_reference[_reference_key(reference)], vocabulary)
        )
        for ticket in by_reference.values()
        if _open(ticket, vocabulary)
    }
    states: dict[tuple[str, int], int] = {}
    cycles: set[frozenset[tuple[str, int]]] = set()

    def visit(key: tuple[str, int], stack: tuple[tuple[str, int], ...]) -> None:
        states[key] = 1
        for child in graph.get(key, ()):
            if states.get(child) == 1:
                cycles.add(frozenset(stack[stack.index(child) :] + (child,)))
            elif states.get(child, 0) == 0:
                visit(child, stack + (child,))
        states[key] = 2

    for key in sorted(graph):
        if states.get(key, 0) == 0:
            visit(key, (key,))
    findings: list[Finding] = []
    for cycle in sorted(cycles, key=lambda item: sorted(item)):
        ordered = sorted(cycle)
        names = ", ".join(f"{effort} ticket {number:02d}" for effort, number in ordered)
        ticket = by_reference[ordered[0]]
        findings.append(
            Finding(
                ticket.path,
                ticket.blocker_line_number or 0,
                f"open blocking cycle: {names}",
                "Rewrite one Blocked by line so the open dependency cycle is removed",
            )
        )
    return tuple(findings)


def _tag_key(ticket: Ticket) -> tuple[int, int, int, str, int]:
    if ticket.tag is not None:
        match = VERSION.fullmatch(ticket.tag)
        if match is not None:
            return (
                int(match.group("major")),
                int(match.group("minor")),
                int(match.group("patch")),
                ticket.effort,
                ticket.number,
            )
    return (sys.maxsize, sys.maxsize, sys.maxsize, ticket.effort, ticket.number)


def _critical_path(release: Ticket, tickets: Sequence[Ticket], vocabulary: Vocabulary) -> tuple[Ticket, ...]:
    by_reference = _reference_map(tickets)
    release_key = _ticket_key(release)
    reachable: set[tuple[str, int]] = set()

    def collect(ticket: Ticket) -> None:
        for reference in ticket.references:
            target = by_reference.get(_reference_key(reference))
            if target is None or not _open(target, vocabulary):
                continue
            key = _ticket_key(target)
            # The release ticket is named in the heading and is never a row, even when a
            # cycle leads back to it; the cycle itself is the lint's finding.
            if key == release_key or key in reachable:
                continue
            reachable.add(key)
            collect(target)

    collect(release)
    selected = [by_reference[key] for key in sorted(reachable) if key in by_reference]
    selected_keys = {_ticket_key(ticket) for ticket in selected}
    dependencies = {
        _ticket_key(ticket): {
            _ticket_key(by_reference[_reference_key(reference)])
            for reference in ticket.references
            if _reference_key(reference) in by_reference
            and _ticket_key(by_reference[_reference_key(reference)]) in selected_keys
            and _open(by_reference[_reference_key(reference)], vocabulary)
        }
        for ticket in selected
    }
    result: list[Ticket] = []
    remaining = {key: set(values) for key, values in dependencies.items()}
    by_key = _reference_map(selected)
    while remaining:
        ready = sorted(key for key, deps in remaining.items() if not deps)
        if not ready:
            # A blocking cycle, reported by the lint: break it at the lowest key so the order completes.
            ready = [min(remaining)]
        for key in ready:
            result.append(by_key[key])
            remaining.pop(key)
            for deps in remaining.values():
                deps.discard(key)
    return tuple(result)


def build_board(
    root: Path,
    today: date | None = None,
    *,
    facts_reader: Callable[[Path], GitFacts | None] = read_git_facts,
) -> Board:
    """Read the tracker once, lint it, and derive every section.

    ``facts_reader`` is the seam a test uses to supply git facts for a fixture
    that is not a repository; the command always reads them from git.
    """

    selected_today = today or datetime.now(UTC).astimezone().date()
    vocabulary = read_vocabulary(root)
    facts = facts_reader(root)
    tickets, shape_findings, efforts = read_tickets(root, facts)
    if not efforts:
        raise TrackerError(
            f"no effort with an issues/ directory under {root / '.scratch'}",
            "Create `.scratch/<effort>/issues/` and add the tracker issues, then retry.",
        )
    findings = _lint(tickets, vocabulary, facts, shape_findings)
    open_tickets = [ticket for ticket in tickets if _open(ticket, vocabulary)]
    status_order = {status: index for index, status in enumerate(vocabulary.open_statuses)}
    standing = sorted(
        (ticket for ticket in open_tickets if ticket.effort == STANDING_EFFORT),
        key=lambda ticket: ticket.number,
    )
    non_standing_open = [ticket for ticket in open_tickets if ticket.effort != STANDING_EFFORT]
    release_tickets = sorted(
        (ticket for ticket in open_tickets if ticket.tag is not None),
        key=_tag_key,
    )
    release = release_tickets[0] if release_tickets else None
    next_tag = release.tag if release is not None else None
    critical = _critical_path(release, tickets, vocabulary) if release is not None else ()
    by_reference = _reference_map(tickets)

    def unblocked(ticket: Ticket) -> bool:
        return not ticket.external_blocker and all(
            _reference_key(reference) in by_reference
            and not _open(by_reference[_reference_key(reference)], vocabulary)
            for reference in ticket.references
        )

    frontier = tuple(
        sorted(
            (
                ticket
                for ticket in open_tickets
                if _offered_now(ticket)
                and ticket.status == "ready-for-agent"
                and unblocked(ticket)
            ),
            key=_ticket_key,
        )
    )
    decisions = tuple(
        sorted(
            (
                ticket
                for ticket in open_tickets
                if _offered_now(ticket) and ticket.status == "ready-for-human"
            ),
            key=lambda ticket: (ticket.opened_date is None, ticket.opened_date or date.max, _ticket_key(ticket)),
        )
    )
    triage = tuple(
        sorted(
            (
                ticket
                for ticket in open_tickets
                if _offered_now(ticket)
                and ticket.status == "needs-triage"
                and ticket.opened_date is not None
                and (selected_today - ticket.opened_date).days > TRIAGE_THRESHOLD_DAYS
            ),
            key=lambda ticket: (ticket.opened_date or date.max, _ticket_key(ticket)),
        )
    )
    groups: list[tuple[str, tuple[Ticket, ...]]] = []
    for effort in sorted({ticket.effort for ticket in non_standing_open}):
        items = tuple(
            sorted(
                (ticket for ticket in non_standing_open if ticket.effort == effort),
                key=lambda ticket: (status_order.get(ticket.status, len(status_order)), ticket.number),
            )
        )
        groups.append((effort, items))
    return Board(
        selected_today,
        vocabulary,
        tuple(tickets),
        len(tickets),
        len(non_standing_open),
        len(efforts),
        len(groups),
        facts is not None,
        findings,
        next_tag,
        release,
        tuple(release_tickets),
        critical,
        frontier,
        decisions,
        triage,
        tuple(groups),
        tuple(standing),
        len(standing),
    )


def _display_number(ticket: Ticket, *, effort: bool) -> str:
    return f"{ticket.effort} {ticket.number:02d}" if effort else f"{ticket.number:02d}"


def _age(ticket: Ticket, today: date) -> str:
    if ticket.opened_date is None:
        return ""
    return str((today - ticket.opened_date).days)


@dataclass(frozen=True, slots=True)
class BlockerCell:
    """The blockers column: references, an external flag, and a fired clause."""

    references: tuple[tuple[str, bool], ...]
    external: bool
    fired: bool


@dataclass(frozen=True, slots=True)
class Row:
    """One table row, the same for both renderers."""

    ticket: Ticket
    number: str
    blockers: BlockerCell
    age: str


@dataclass(frozen=True, slots=True)
class Section:
    """One board section: a heading, an optional note under it, rows or an empty text."""

    title: str
    note: str | None
    rows: tuple[Row, ...]
    empty: str
    columns: tuple[str, ...] = COLUMNS


def _blocker_cell(
    ticket: Ticket, by_reference: dict[tuple[str, int], Ticket], vocabulary: Vocabulary
) -> BlockerCell:
    references: list[tuple[str, bool]] = []
    for reference in ticket.references:
        target = by_reference.get(_reference_key(reference))
        closed = target is not None and not _open(target, vocabulary)
        item = (f"{reference.effort} {reference.number:02d}", closed)
        if item not in references:
            references.append(item)
    return BlockerCell(tuple(references), ticket.external_blocker, ticket.fired)


def _rows(
    tickets: Iterable[Ticket],
    board: Board,
    *,
    effort: bool,
    mark_keys: frozenset[tuple[str, int]] = frozenset(),
) -> tuple[Row, ...]:
    by_reference = _reference_map(board.tickets)
    rows: list[Row] = []
    for ticket in tickets:
        number = _display_number(ticket, effort=effort)
        if _ticket_key(ticket) in mark_keys:
            number = CRITICAL_MARK + number
        rows.append(
            Row(
                ticket,
                number,
                _blocker_cell(ticket, by_reference, board.vocabulary),
                _age(ticket, board.today),
            )
        )
    return tuple(rows)


def _critical_section(board: Board) -> Section:
    if board.release_ticket is None:
        return Section("Critical path", None, (), NO_RELEASE_TICKET)
    note = None
    if len(board.release_tickets) > 1:
        names = ", ".join(
            f"{ticket.effort} ticket {ticket.number:02d}" for ticket in board.release_tickets
        )
        note = f"Several open release tickets: {names}."
    title = (
        f"Critical path to {board.next_tag} — "
        f"{board.release_ticket.effort} ticket {board.release_ticket.number:02d}"
    )
    return Section(title, note, _rows(board.critical_path, board, effort=True), EMPTY)


def board_sections(board: Board) -> tuple[Section, ...]:
    """The four cross-effort sections in the board's order, shared by both renderers."""

    critical_keys = frozenset(_ticket_key(ticket) for ticket in board.critical_path)
    frontier_note = (
        CRITICAL_MARK_NOTE
        if any(_ticket_key(ticket) in critical_keys for ticket in board.frontier)
        else None
    )
    return (
        _critical_section(board),
        Section(
            "Frontier",
            frontier_note,
            _rows(board.frontier, board, effort=True, mark_keys=critical_keys),
            EMPTY,
            FRONTIER_COLUMNS,
        ),
        Section("Decisions owed", None, _rows(board.decisions_owed, board, effort=True), EMPTY),
        Section(TRIAGE_TITLE, None, _rows(board.triage_owed, board, effort=True), EMPTY),
    )


def listing_sections(board: Board) -> tuple[Section, ...]:
    """One section per effort with open tickets, rows numbered within the effort."""

    return tuple(
        Section(f"{effort} ({len(tickets)} open)", None, _rows(tickets, board, effort=False), EMPTY)
        for effort, tickets in board.listing
    )


def standing_section(board: Board) -> Section:
    """The open standing tickets, kept apart from ordinary effort listings."""

    return Section(
        f"Standing ({board.standing_open})", None, _rows(board.standing, board, effort=False), EMPTY
    )


def summary_line(board: Board) -> str:
    """The one-line count the board's header and the command's stdout share."""

    return (
        f"{board.open_tickets} open across {board.efforts_open} efforts "
        f"and {board.standing_open} standing ({board.total_tickets} tickets read in "
        f"{board.efforts_read} efforts)."
    )


def _git_facts_line(board: Board) -> str:
    if board.git_facts_present:
        return "Git facts: present (opened dates and uncommitted paths read)."
    return "Git facts: absent (ages blank and the uncommitted-claim rule was not checked)."


def _finding_line(finding: Finding) -> str:
    return f"{finding.path}:{finding.line}: {finding.problem}. Fix: {finding.fix}."


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _markdown_blockers(cell: BlockerCell) -> str:
    values = [f"~~{label}~~" if closed else label for label, closed in cell.references]
    if cell.external:
        values.append(EXTERNAL)
    if cell.fired:
        values.append("fired")
    return ", ".join(values) or EMPTY


def _markdown_header(columns: Sequence[str]) -> str:
    """Build a Markdown table header for the section's columns."""

    return "| " + " | ".join(columns) + " |"


def _markdown_separator(columns: Sequence[str]) -> str:
    """Build a Markdown table separator for the section's columns."""

    return "| " + " | ".join("---" for _ in columns) + " |"


def _markdown_section(section: Section, level: str) -> list[str]:
    lines = [f"{level} {section.title}"]
    if section.note is not None:
        lines.extend([section.note, ""])
    if not section.rows:
        lines.append(section.empty)
        return lines
    lines.extend([_markdown_header(section.columns), _markdown_separator(section.columns)])
    for row in section.rows:
        cells = {
            "number": row.number,
            "title": row.ticket.title,
            "status": row.ticket.status,
            "blockers": _markdown_blockers(row.blockers),
            "age (days)": row.age,
        }
        lines.append(
            "| " + " | ".join(_markdown_cell(cells[column]) for column in section.columns) + " |"
        )
    return lines


def render_markdown(board: Board) -> str:
    """Render the board as Markdown: the header, the findings, then every section."""

    lines = [
        "# Tracker board",
        "",
        f"Generated by `python3 -m tools.tracker` on {board.today.isoformat()}.",
        summary_line(board),
        _git_facts_line(board),
        "",
        "## Lint findings",
    ]
    if board.findings:
        lines.extend(f"- {_finding_line(finding)}" for finding in board.findings)
    else:
        lines.append(EMPTY)
    for section in board_sections(board):
        lines.append("")
        lines.extend(_markdown_section(section, "##"))
    lines.extend(["", "## Open by effort"])
    if not board.listing:
        lines.append(EMPTY)
    for section in listing_sections(board):
        lines.append("")
        lines.extend(_markdown_section(section, "###"))
    lines.append("")
    lines.extend(_markdown_section(standing_section(board), "##"))
    return "\n".join(lines) + "\n"


def _html(value: str) -> str:
    return html.escape(value, quote=True)


def _html_href(ticket: Ticket) -> str:
    return _html(ticket.path.relative_to(Path(".scratch")).as_posix())


def _html_blockers(cell: BlockerCell) -> str:
    values = [
        f"<del>{_html(label)}</del>" if closed else _html(label) for label, closed in cell.references
    ]
    if cell.external:
        values.append(_html(EXTERNAL))
    if cell.fired:
        values.append(_html("fired"))
    return ", ".join(values) or _html(EMPTY)


def _html_header(columns: Sequence[str]) -> str:
    """Build an HTML table header for the section's columns."""

    return "<thead><tr>" + "".join(f"<th>{column}</th>" for column in columns) + "</tr></thead>"


def _html_section(section: Section, level: str) -> list[str]:
    lines = [f"<{level}>{_html(section.title)}</{level}>"]
    if section.note is not None:
        lines.append(f"<p>{_html(section.note)}</p>")
    if not section.rows:
        lines.append(f"<p>{_html(section.empty)}</p>")
        return lines
    lines.extend(["<table>", _html_header(section.columns), "<tbody>"])
    for row in section.rows:
        cells = {
            "number": f'<td><a href="{_html_href(row.ticket)}">{_html(row.number)}</a></td>',
            "title": f"<td>{_html(row.ticket.title)}</td>",
            "status": f'<td data-status="{_html(row.ticket.status)}">{_html(row.ticket.status)}</td>',
            "blockers": f"<td>{_html_blockers(row.blockers)}</td>",
            "age (days)": f"<td>{_html(row.age)}</td>",
        }
        lines.append("<tr>" + "".join(cells[column] for column in section.columns) + "</tr>")
    lines.extend(["</tbody>", "</table>"])
    return lines


def render_html(board: Board) -> str:
    """Render the same board as one self-contained page: no script, no external reference."""

    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Tracker board</title>",
        "<style>",
        HTML_STYLE,
        "</style>",
        "</head>",
        "<body>",
        "<h1>Tracker board</h1>",
        f'<p data-kind="metadata">{_html(f"Generated by python3 -m tools.tracker on {board.today.isoformat()}.")}</p>',
        f"<p>{_html(summary_line(board))}</p>",
        f'<p data-kind="metadata">{_html(_git_facts_line(board))}</p>',
        "<section>",
        "<h2>Lint findings</h2>",
    ]
    if board.findings:
        lines.append("<ul>")
        lines.extend(f"<li>{_html(_finding_line(finding))}</li>" for finding in board.findings)
        lines.append("</ul>")
    else:
        lines.append(f"<p>{_html(EMPTY)}</p>")
    lines.append("</section>")
    for section in board_sections(board):
        lines.append("<section>")
        lines.extend(_html_section(section, "h2"))
        lines.append("</section>")
    lines.extend(["<section>", "<h2>Open by effort</h2>"])
    if not board.listing:
        lines.append(f"<p>{_html(EMPTY)}</p>")
    for section in listing_sections(board):
        lines.extend(_html_section(section, "h3"))
    lines.extend(["</section>", "<section>"])
    lines.extend(_html_section(standing_section(board), "h2"))
    lines.extend(["</section>", "</body>", "</html>"])
    return "\n".join(lines) + "\n"


def _write_outputs(outputs: Sequence[tuple[Path, str, str]]) -> None:
    """Write every output or none: each to a sibling temporary file, then all moved into place."""

    for path, label, _ in outputs:
        if path.exists() and not path.is_file():
            raise TrackerError(
                f"cannot write the {label} at {path}: the path is not a file",
                f"Move or remove {path}, or choose another checkout with `--root DIR`, then retry.",
            )
    temporaries: list[tuple[Path, Path]] = []
    try:
        for path, label, text in outputs:
            temporary = path.with_name(path.name + ".tmp")
            try:
                temporary.write_text(text, encoding="utf-8")
            except OSError as error:
                raise TrackerError(
                    f"cannot write the {label} at {path}: {error}",
                    "Choose a writable checkout with `--root DIR`, then retry.",
                ) from error
            temporaries.append((temporary, path))
        for temporary, path in temporaries:
            temporary.replace(path)
    finally:
        for temporary, _ in temporaries:
            temporary.unlink(missing_ok=True)


def _parse_today(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tools.tracker")
    parser.add_argument("--root", type=Path, metavar="DIR")
    parser.add_argument("--today", type=_parse_today, metavar="YYYY-MM-DD")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--check", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    today: date | None = None,
) -> int:
    """Build the board, optionally write its outputs, and print the findings.

    ``--check`` skips both output writes while preserving findings and the strict exit code.
    """

    options = _parser().parse_args(argv)
    selected_root = (options.root or root or DEFAULT_ROOT).expanduser().resolve()
    selected_today = options.today or today
    try:
        board = build_board(selected_root, selected_today)
        if not options.check:
            _write_outputs(
                (
                    (selected_root / BOARD_PATH, "board", render_markdown(board)),
                    (selected_root / PAGE_PATH, "page", render_html(board)),
                )
            )
    except TrackerError as error:
        print(f"python3 -m tools.tracker: {error.problem} Fix: {error.fix}", file=sys.stderr)
        return 1
    for finding in board.findings:
        print(_finding_line(finding))
    print(summary_line(board))
    return 1 if options.strict and board.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
