"""Classify rated turns and write a deterministic candidate packet.

The command prints only its four stage rows and writes only the page and its
manifest.  All refusals are checked before a write starts, the database read
uses one statement, and turn text appears only in the page.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal
from zoneinfo import ZoneInfo

from gideon import guardrail
from gideon.api import stamp
from gideon.host import backupset, site
from gideon.host.report import Problem, StageResult, print_stage, refusal
from gideon.host.sysio import Host, PathLike, RealHost
from gideon.improvement import owuisnapshot
from gideon.improvement.snapshot import RatedTurn, SnapshotReading, SnapshotSource

type Bucket = Literal["refused", "stamped", "answered", "unreadable"]

BUCKETS: Final[tuple[Bucket, ...]] = (
    "refused",
    "stamped",
    "answered",
    "unreadable",
)
PACKET_FORMAT: Final[int] = 1
MONTH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_TIME_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"
_INSTALL_HOME: Final[Path] = Path("/opt/gideon")
_OUTSIDE_FIX: Final[str] = (
    "Name an absolute directory under /root, such as /root/candidates/<YYYY-MM>, then retry."
)
_CHECKOUT_FIX: Final[str] = (
    "Name a directory outside every checkout, such as /root/candidates/<YYYY-MM>, then retry."
)
_EMPTY_FIX: Final[str] = (
    "Name an absent path or an empty directory, since a packet is written whole, then retry."
)
_OWNER_FIX: Final[str] = "Create the directory as root, or name an absent path, then retry."
_ROOT_FIX: Final[str] = "Run sudo python3 -m gideon eval candidates --out <dir>, then retry."
_SITE_FIX: Final[str] = "Correct the site file, then retry."
_MONTH_FIX: Final[str] = "Supply --month YYYY-MM, then retry."
_WRITE_FIX: Final[str] = (
    "Name an absent path or an empty directory outside every checkout, then retry."
)
_COMMAND: Final[str] = "eval candidates"
# The page explains itself to the person reading it at a shell, since the
# runbook may not be at hand; the text is fixed, so the page stays a pure
# function of its rows.
_GUIDE: Final[tuple[str, ...]] = (
    "How to read this page",
    "- Each section below is one answer a user rated thumbs-down this month: the question",
    "  they asked, then the answer they rated, as it stood when they rated it.",
    "- The bucket on a section's first line is decided by code from the stored answer:",
    "    refused     ends with that guardrail's fixed refusal: a report that the refusal",
    "                was wrong, for the guardrail review rather than a candidate",
    "    stamped     ends with the citation stamp: likely a citation complaint, which",
    "                General cannot check; a candidate for Research",
    "    answered    any other answer: a plain candidate",
    "    unreadable  the rated message is missing, empty, or not the assistant's: report",
    "                its ids from manifest.json, never its text",
    "- A candidate is a legal research question you write in your own words, with no",
    "  client, matter, case fact, or name. Add each to /root/candidates/candidates.txt",
    "  as its own block under a line '# reviewed <role id> <YYYY-MM-DD>'.",
    "- Keep this page on the box and delete its directory once its candidates are",
    "  written. The procedure is docs/runbooks/feedback-packet.md in the release checkout.",
)


@dataclass(frozen=True, slots=True)
class BucketedTurn:
    """A rated turn's packet category and optional refusal family."""

    turn: RatedTurn
    bucket: Bucket
    family: str | None


def bucket_for(turn: RatedTurn) -> tuple[Bucket, str | None]:
    """Classify the stored answer using the release's fixed response text."""

    if turn.answer is None or turn.answer_role != "assistant":
        return "unreadable", None

    normalized = " ".join(turn.answer.split())
    if not normalized:
        return "unreadable", None
    # A refusal is the whole answer, or follows a stream's released prefix and
    # the separator, which normalises to one space.
    for family, refusal_text in guardrail.REFUSAL_BY_FAMILY.items():
        fixed = " ".join(refusal_text.split())
        if normalized == fixed or normalized.endswith(" " + fixed):
            return "refused", family
    if stamp.is_stamped(turn.answer):
        return "stamped", None
    return "answered", None


def _month_start(month: str, zone: str) -> datetime:
    if MONTH_PATTERN.fullmatch(month) is None:
        raise ValueError("month must use YYYY-MM with a valid month")
    year = int(month[:4])
    number = int(month[5:])
    return datetime(year, number, 1, tzinfo=ZoneInfo(zone))


def month_bounds(month: str, zone: str) -> tuple[int, int]:
    """Return the month's local midnights as half-open epoch-second bounds."""

    start = _month_start(month, zone)
    if start.month == 12:
        next_start = datetime(start.year + 1, 1, 1, tzinfo=start.tzinfo)
    else:
        next_start = datetime(start.year, start.month + 1, 1, tzinfo=start.tzinfo)
    return int(start.timestamp()), int(next_start.timestamp())


def default_month(zone: str, now: float) -> str:
    """Return the previous calendar month at the supplied instant and zone."""

    current = datetime.fromtimestamp(now, ZoneInfo(zone))
    if current.month == 1:
        return f"{current.year - 1:04d}-12"
    return f"{current.year:04d}-{current.month - 1:02d}"


type Resolve = Callable[[str], str]


def _resolved(path: PathLike, resolve: Resolve) -> Path:
    # Writes follow links, so every comparison is between resolved paths: an
    # alias of a protected directory is that directory.
    return Path(resolve(os.path.normpath(os.fspath(path))))


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def _under_excluded_root(path: Path, checkout: Path, resolve: Resolve) -> bool:
    sources = [root.source for root in backupset.inventory_roots(os.fspath(checkout))]
    sources += [backupset.STAGING, os.fspath(_INSTALL_HOME)]
    return any(_contains(_resolved(source, resolve), path) for source in sources)


def _inside_checkout(path: Path, host: Host) -> bool:
    current = path
    while True:
        if host.exists(current / ".git"):
            return True
        if current.parent == current:
            return False
        current = current.parent


def judge_out(
    path: PathLike,
    checkout: PathLike,
    host: Host,
    resolve: Resolve = os.path.realpath,
) -> Problem | None:
    """Refuse packet destinations inside protected roots or unusable folders."""

    if not Path(os.fspath(path)).is_absolute():
        return Problem("The packet output path must be absolute.", _OUTSIDE_FIX)

    output = _resolved(path, resolve)
    if _under_excluded_root(output, Path(os.fspath(checkout)), resolve):
        return Problem(
            "The packet would be carried by a backup set or the install home.",
            _OUTSIDE_FIX,
        )
    try:
        if _inside_checkout(output, host):
            return Problem("The packet would be inside a checkout.", _CHECKOUT_FIX)
    except OSError:
        return Problem("The output path could not be checked for a checkout.", _CHECKOUT_FIX)

    try:
        if not host.exists(output):
            return None
        details = host.stat(output)
        if not stat.S_ISDIR(details.st_mode):
            return Problem("The packet output path is not an empty directory.", _EMPTY_FIX)
        if host.listdir(output):
            return Problem("The packet output directory is not empty.", _EMPTY_FIX)
    except OSError:
        return Problem("The packet output directory could not be inspected.", _EMPTY_FIX)
    if details.st_uid != 0:
        return Problem("The empty packet output directory is not root-owned.", _OWNER_FIX)
    return None


def page_name(month: str) -> str:
    """Return the deterministic page filename for a validated month label."""

    if MONTH_PATTERN.fullmatch(month) is None:
        raise ValueError("month must use YYYY-MM with a valid month")
    return f"candidates-{month}.txt"


def _ordered(turns: tuple[BucketedTurn, ...]) -> tuple[BucketedTurn, ...]:
    bucket_order = {bucket: index for index, bucket in enumerate(BUCKETS)}
    return tuple(
        sorted(
            turns,
            key=lambda item: (
                bucket_order[item.bucket],
                item.turn.record.message_id,
                item.turn.feedback_id,
            ),
        )
    )


def _rated_at(turn: RatedTurn) -> str:
    return datetime.fromtimestamp(turn.record.created_at, UTC).strftime(_TIME_FORMAT)


def _page_facts(page_text: str) -> tuple[int, str]:
    return len(page_text.splitlines()), hashlib.sha256(page_text.encode("utf-8")).hexdigest()


def _counts(turns: tuple[BucketedTurn, ...]) -> dict[Bucket, int]:
    return {bucket: sum(item.bucket == bucket for item in turns) for bucket in BUCKETS}


def render_page(
    month: str,
    zone: str,
    ratings: SnapshotReading,
    turns: tuple[BucketedTurn, ...],
) -> str:
    """Render the text page from the month, timezone, ratings, and turns."""

    ordered = _ordered(turns)
    down = sum(turn.record.rating == "down" for turn in ratings.turns)
    counts = ", ".join(f"{bucket} {count}" for bucket, count in _counts(ordered).items())
    header = "\n".join(
        (
            f"GIDEON candidate packet | month {month} | timezone {zone} | format {PACKET_FORMAT}",
            "Never copy a line from this page into a candidate case; write the question in your own words.",
            f"Ratings {len(ratings.turns)}, rated down {down}, skipped {ratings.skipped}: {counts}",
            "",
            *_GUIDE,
        )
    )
    sections: list[str] = []
    for index, item in enumerate(ordered, start=1):
        turn = item.turn
        bucket_label: str = item.bucket
        if item.family is not None:
            bucket_label += f" (guardrail: {item.family})"
        sections.append(
            "\n".join(
                (
                    f"== turn {index} of {len(ordered)} | {bucket_label} | rated {_rated_at(turn)}",
                    f"chat: {turn.record.chat_id} | message: {turn.record.message_id} | model: {turn.record.model_id}",
                    f"feedback: {turn.feedback_id}",
                    "-- question --",
                    "(absent)" if item.bucket == "unreadable" or not turn.question else turn.question,
                    "-- answer --",
                    "(absent)" if item.bucket == "unreadable" or not turn.answer else turn.answer,
                )
            )
        )
    if not sections:
        return header + "\n"
    return header + "\n\n" + "\n\n".join(sections) + "\n"


def manifest(
    month: str,
    zone: str,
    ratings: SnapshotReading,
    turns: tuple[BucketedTurn, ...],
    page_text: str,
) -> dict[str, object]:
    """Build the text-free metadata for a rendered candidate packet page."""

    ordered = _ordered(turns)
    counts = _counts(ordered)
    lines, digest = _page_facts(page_text)
    return {
        "packet_format": PACKET_FORMAT,
        "month": month,
        "timezone": zone,
        "page": {"name": page_name(month), "lines": lines, "sha256": digest},
        "ratings": len(ratings.turns),
        "rated_down": sum(turn.record.rating == "down" for turn in ratings.turns),
        "skipped": ratings.skipped,
        "counts": counts,
        "turns": [
            {
                "bucket": item.bucket,
                "family": item.family,
                "chat_id": item.turn.record.chat_id,
                "message_id": item.turn.record.message_id,
                "model_id": item.turn.record.model_id,
                "feedback_id": item.turn.feedback_id,
                "rated_at": _rated_at(item.turn),
            }
            for item in ordered
        ],
    }


def _preflight_refusal(problem: str, fix: str) -> int:
    print(refusal(_COMMAND, problem, fix), file=sys.stderr)
    return 1


def _stage_refusal(stage: str, problem: str, fix: str) -> int:
    print_stage(StageResult(stage, False, problem, fix))
    return 1


def _bucketed_down(reading: SnapshotReading) -> tuple[BucketedTurn, ...]:
    result: list[BucketedTurn] = []
    for turn in reading.turns:
        if turn.record.rating != "down":
            continue
        bucket, family = bucket_for(turn)
        result.append(BucketedTurn(turn, bucket, family))
    return _ordered(tuple(result))


def _write_packet(
    destination: Path,
    page_path: Path,
    page_text: str,
    manifest_text: str,
    host: Host,
) -> None:
    if host.exists(destination):
        details = host.stat(destination)
        if (
            not stat.S_ISDIR(details.st_mode)
            or host.listdir(destination)
            or details.st_uid != 0
        ):
            raise OSError("packet output directory changed after it was checked")
        host.chmod(destination, 0o700)
    else:
        host.mkdir(destination, mode=0o700)
    host.write_text(page_path, page_text, encoding="utf-8", mode=0o600)
    host.write_text(
        destination / "manifest.json",
        manifest_text,
        encoding="utf-8",
        mode=0o600,
    )


def run_candidates(
    args: argparse.Namespace,
    *,
    host: Host | None = None,
    checkout_root: PathLike | None = None,
    rendered_dir: PathLike = "/etc/gideon/rendered",
    site_path: PathLike = "/etc/gideon/site.yaml",
    source_factory: Callable[[Host, PathLike], SnapshotSource] | None = None,
    now: Callable[[], float] = time.time,
    resolve: Resolve = os.path.realpath,
) -> int:
    """Read one office month and write its candidate packet."""

    checkout = Path(__file__).parents[2] if checkout_root is None else Path(checkout_root)
    io = RealHost() if host is None else host
    if io.geteuid() != 0:
        return _preflight_refusal("this command must run as root", _ROOT_FIX)

    loaded = site.load_site(Path(site_path), host=io)
    if loaded.errors or loaded.config is None:
        detail = site.render_errors(loaded.errors) or "site file could not be loaded."
        return _preflight_refusal(detail, _SITE_FIX)
    zone = loaded.config.office.timezone

    supplied_month = getattr(args, "month", None)
    if supplied_month is None:
        month = default_month(zone, now())
    elif not isinstance(supplied_month, str) or MONTH_PATTERN.fullmatch(supplied_month) is None:
        return _preflight_refusal("month must use YYYY-MM with a valid month", _MONTH_FIX)
    else:
        month = supplied_month
    try:
        start, end = month_bounds(month, zone)
    except (OverflowError, ValueError):
        return _preflight_refusal("month must use YYYY-MM with a valid month", _MONTH_FIX)

    supplied_out = getattr(args, "out", None)
    if not isinstance(supplied_out, (str, os.PathLike)):
        return _preflight_refusal("an output directory is required", _OUTSIDE_FIX)
    # The judgment sees the path as typed, so a relative one is refused
    # before anything resolves it against the working directory.
    output_problem = judge_out(supplied_out, checkout, io, resolve)
    if output_problem is not None:
        return _preflight_refusal(output_problem.problem, output_problem.fix)
    # The packet is written where the judgment looked: the resolved path.
    destination = _resolved(supplied_out, resolve)

    rendered = Path(rendered_dir)
    selected_source = (
        owuisnapshot.source(io, rendered)
        if source_factory is None
        else source_factory(io, rendered)
    )
    print_stage(StageResult("load", True, f"month {month} in {zone}: {start}..{end}", ""))

    reading = selected_source.read(start, end)
    if isinstance(reading, Problem):
        return _stage_refusal("read", reading.problem, reading.fix)

    turns = _bucketed_down(reading)
    unreadable = sum(item.bucket == "unreadable" for item in turns)
    read_detail = (
        f"{len(reading.turns)} ratings, "
        f"{sum(turn.record.rating == 'down' for turn in reading.turns)} rated down, "
        f"{unreadable} unreadable"
    )
    if reading.skipped:
        read_detail += f", {reading.skipped} skipped"
    print_stage(StageResult("read", True, read_detail, ""))

    page_text = render_page(month, zone, reading, turns)
    page_file = page_name(month)
    page_path = destination / page_file
    manifest_text = (
        json.dumps(manifest(month, zone, reading, turns, page_text), sort_keys=True, indent=2)
        + "\n"
    )
    line_count, digest = _page_facts(page_text)
    try:
        _write_packet(destination, page_path, page_text, manifest_text, io)
    except (OSError, UnicodeError, ValueError):
        return _stage_refusal("write", "packet files could not be written", _WRITE_FIX)
    print_stage(
        StageResult(
            "write",
            True,
            f"{page_path}: {line_count} lines, sha256 {digest}; manifest.json beside it",
            "",
        )
    )

    counts = _counts(turns)
    families: dict[str, int] = {}
    for item in turns:
        if item.family is not None:
            families[item.family] = families.get(item.family, 0) + 1
    refused_detail = str(counts["refused"])
    if families:
        refused_detail += " (" + ", ".join(
            f"{family} {count}" for family, count in sorted(families.items())
        ) + ")"
    summary = (
        f"refused {refused_detail}, stamped {counts['stamped']}, "
        f"answered {counts['answered']}, unreadable {counts['unreadable']}"
    )
    print_stage(StageResult("summary", True, summary, ""))
    return 0
