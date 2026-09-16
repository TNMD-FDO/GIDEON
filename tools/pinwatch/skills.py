"""Records and hashing rules for the repository's vendored development skills.

This module is deliberately standard-library-only.  It owns the lock the
tripwire and the clone checker read, the provenance line the Matt Pocock pin
reads, and the folder-hash algorithm shared with the ``skills`` CLI, so the pin
watch and the hosted tripwire validate the same shapes.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

MATT_SOURCE: Final = "mattpocock/skills"
SKILLS_LOCK_PATH: Final = "skills-lock.json"
PROVENANCE_PATH: Final = "docs/agents/tooling.md"
SKILLS_ROOT: Final = ".claude/skills"
MATT_PIN_ID: Final = "skills.matt-pocock"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE = re.compile(
    r"^Provenance today: Matt Pocock skills at upstream `main` commit "
    r"`(?P<commit>[0-9a-f]{7,40})` \((?P<date>\d{4}-\d{2}-\d{2})\)\.$"
)
_PROVENANCE_PREFIX = "Provenance today:"
_PUNCTUATION_ORDER = "_-,;:!?."
_SKILL_RECORD_FIX = (
    "Inspect the record and re-run the §3 recipe in docs/agents/tooling.md."
)
_PROVENANCE_FIX = (
    "Restore the one-line shape of the provenance line in "
    "docs/agents/tooling.md §2."
)


class SkillsRecordError(ValueError):
    """A skill record cannot be trusted by the watch or the tripwire."""

    def __init__(self, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.fix = fix

    def __str__(self) -> str:
        return f"{self.problem} Fix: {self.fix}"


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One entry from ``skills-lock.json``."""

    name: str
    source: str
    ref: str | None
    skill_path: str
    computed_hash: str


@dataclass(frozen=True, slots=True)
class SkillsLock:
    """The ordered, validated entries from ``skills-lock.json``."""

    entries: tuple[SkillEntry, ...]


@dataclass(frozen=True, slots=True)
class Provenance:
    """The machine-readable provenance sentence in tooling.md §2."""

    matt_commit: str
    matt_date: str


class _ChangeLike(Protocol):
    @property
    def key_path(self) -> str: ...

    @property
    def old(self) -> str: ...

    @property
    def new(self) -> str: ...


class _BumpLike(Protocol):
    @property
    def lock(self) -> str: ...

    @property
    def changes(self) -> Sequence[_ChangeLike]: ...


def _record_error(problem: str) -> SkillsRecordError:
    return SkillsRecordError(problem, _SKILL_RECORD_FIX)


def _mapping(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _record_error(f"skills-lock.json {description} must be an object")
    return value


def _required_string(
    value: dict[str, object], key: str, entry_name: str
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise _record_error(
            f"skills-lock.json entry {entry_name!r} requires a non-empty {key!r}"
        )
    return item


def load_skills_lock(text: str) -> SkillsLock:
    """Parse and validate a skills lock without changing its entry order."""

    try:
        document = json.loads(text)
    except (json.JSONDecodeError, TypeError) as error:
        raise _record_error(f"skills-lock.json is not valid JSON: {error}") from error

    root = _mapping(document, "must be an object")
    if set(root) != {"version", "skills"}:
        raise _record_error(
            "skills-lock.json must contain only the version and skills keys"
        )
    version = root.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise _record_error("skills-lock.json version must be 1")
    skills = _mapping(root.get("skills"), "skills must be an object")
    if not skills:
        raise _record_error("skills-lock.json skills must not be empty")

    entries: list[SkillEntry] = []
    for name, raw_entry in skills.items():
        if not isinstance(name, str) or not name:
            raise _record_error("skills-lock.json skill names must be non-empty strings")
        entry = _mapping(raw_entry, f"entry {name!r} must be an object")
        allowed = {"source", "ref", "sourceType", "skillPath", "computedHash"}
        if set(entry) - allowed:
            raise _record_error(
                f"skills-lock.json entry {name!r} has an unknown key"
            )
        source = _required_string(entry, "source", name)
        source_type = entry.get("sourceType")
        if source_type != "github":
            raise _record_error(
                f"skills-lock.json entry {name!r} sourceType must be 'github'"
            )
        skill_path = _required_string(entry, "skillPath", name)
        computed_hash = _required_string(entry, "computedHash", name)
        if _HEX64.fullmatch(computed_hash) is None:
            raise _record_error(
                f"skills-lock.json entry {name!r} computedHash must be 64 lowercase hex characters"
            )
        if "ref" in entry:
            raw_ref = entry["ref"]
            if not isinstance(raw_ref, str) or not raw_ref:
                raise _record_error(
                    f"skills-lock.json entry {name!r} ref must be a non-empty string when present"
                )
            ref: str | None = raw_ref
        else:
            ref = None
        entries.append(SkillEntry(name, source, ref, skill_path, computed_hash))

    return SkillsLock(tuple(entries))


def parse_provenance(text: str) -> Provenance:
    """Parse the one machine-readable provenance line from tooling.md §2."""

    candidates = [
        line
        for line in text.splitlines()
        if line.startswith(_PROVENANCE_PREFIX)
    ]
    if len(candidates) != 1:
        raise SkillsRecordError(
            "docs/agents/tooling.md must contain exactly one provenance line",
            _PROVENANCE_FIX,
        )
    match = _PROVENANCE.fullmatch(candidates[0])
    if match is None:
        raise SkillsRecordError(
            "the provenance line in docs/agents/tooling.md has the wrong shape",
            _PROVENANCE_FIX,
        )
    return Provenance(match.group("commit"), match.group("date"))


def _provenance_line(text: str) -> tuple[list[str], int, str]:
    lines = text.splitlines(keepends=True)
    candidates = [
        (index, line)
        for index, line in enumerate(lines)
        if line.rstrip("\r\n").startswith(_PROVENANCE_PREFIX)
    ]
    if len(candidates) != 1:
        raise SkillsRecordError(
            "docs/agents/tooling.md must contain exactly one provenance line",
            _PROVENANCE_FIX,
        )
    index, line = candidates[0]
    return lines, index, line


def patch_provenance(
    text: str, changes: Sequence[_ChangeLike]
) -> str:
    """Apply anchored commit/date changes to the one provenance line."""

    provenance = parse_provenance(text)
    lines, line_index, original_line = _provenance_line(text)
    line_body = original_line.rstrip("\r\n")
    newline = original_line[len(line_body) :]
    values = {
        "matt-pocock.commit": provenance.matt_commit,
        "matt-pocock.date": provenance.matt_date,
    }
    seen: set[str] = set()
    expected_line = line_body
    for change in changes:
        if change.key_path not in values:
            raise _record_error(
                f"provenance bump has unsupported key path {change.key_path!r}"
            )
        if change.key_path in seen:
            raise _record_error(
                f"provenance bump contains a duplicate key path: {change.key_path}"
            )
        seen.add(change.key_path)
        current = values[change.key_path]
        if current != change.old:
            raise _record_error(
                f"provenance bump is anchored to {change.key_path}={change.old!r}, "
                f"but the record contains {current!r}"
            )
        if change.key_path == "matt-pocock.commit":
            old_token = f"`{current}`"
            new_token = f"`{change.new}`"
        else:
            old_token = f"({current})"
            new_token = f"({change.new})"
        if expected_line.count(old_token) != 1:
            raise _record_error(
                f"provenance key path is not uniquely represented: {change.key_path}"
            )
        expected_line = expected_line.replace(old_token, new_token, 1)
        values[change.key_path] = change.new

    lines[line_index] = expected_line + newline
    patched = "".join(lines)
    parsed = parse_provenance(patched)
    if parsed.matt_commit != values["matt-pocock.commit"]:
        raise _record_error("patched provenance commit did not re-parse")
    if parsed.matt_date != values["matt-pocock.date"]:
        raise _record_error("patched provenance date did not re-parse")
    return patched


def _record_value(record: Provenance, key_path: str) -> str | None:
    if key_path == "matt-pocock.commit":
        return record.matt_commit
    if key_path == "matt-pocock.date":
        return record.matt_date
    return None


def record_values(record_text: str, bump: _BumpLike) -> tuple[str, ...] | None:
    """Return the values a skill record holds at a bump's key paths, or None."""

    if bump.lock != PROVENANCE_PATH:
        return None
    values: list[str] = []
    try:
        record = parse_provenance(record_text)
        for change in bump.changes:
            value = _record_value(record, change.key_path)
            if value is None:
                return None
            values.append(value)
    except SkillsRecordError:
        return None
    return tuple(values)


def carries(record_text: str, bump: _BumpLike) -> bool:
    """Return whether a record holds every new value of a bump."""

    values = record_values(record_text, bump)
    return values is not None and all(
        value == change.new for value, change in zip(values, bump.changes, strict=True)
    )


def _clone_skill_directory(clone_root: Path, entry: SkillEntry) -> Path | None:
    skill_path = Path(entry.skill_path)
    if (
        skill_path.is_absolute()
        or skill_path.name != "SKILL.md"
        or ".." in skill_path.parts
    ):
        return None
    return clone_root / skill_path.parent


def clone_findings(
    lock: SkillsLock,
    source: str,
    clone_root: Path,
    skills_root: Path,
) -> tuple[tuple[str, str], ...]:
    """Compare lock, upstream clone, and installed folders for one source.

    Rows preserve lock order and contain ``(entry name, status)``.  Statuses
    are ``absent from clone and installed``, ``absent from clone``, ``absent
    from installed``, ``installed differs from clone``, ``installed and lock
    differ from clone``, ``lock differs from clone``, or ``match``.
    """

    rows: list[tuple[str, str]] = []
    for entry in lock.entries:
        if entry.source != source:
            continue
        clone_directory = _clone_skill_directory(clone_root, entry)
        installed_directory = skills_root / entry.name
        clone_exists = clone_directory is not None and clone_directory.is_dir()
        installed_exists = installed_directory.is_dir()
        if not clone_exists or not installed_exists:
            if not clone_exists and not installed_exists:
                status = "absent from clone and installed"
            elif not clone_exists:
                status = "absent from clone"
            else:
                status = "absent from installed"
        else:
            assert clone_directory is not None
            clone_hash = folder_hash(clone_directory)
            installed_hash = folder_hash(installed_directory)
            if installed_hash != clone_hash:
                if entry.computed_hash != clone_hash:
                    status = "installed and lock differ from clone"
                else:
                    status = "installed differs from clone"
            elif entry.computed_hash != clone_hash:
                status = "lock differs from clone"
            else:
                status = "match"
        rows.append((entry.name, status))
    return tuple(rows)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(
    argv: Sequence[str] | None = None,
    *,
    checkout_root: Path | None = None,
) -> int:
    """Print clone-check rows and return success only when all rows match."""

    parser = argparse.ArgumentParser(prog="python -m tools.pinwatch.skills")
    parser.add_argument("--source", required=True)
    parser.add_argument("clone_dir", type=Path)
    options = parser.parse_args(argv)
    root = checkout_root or _repository_root()
    try:
        lock_text = (root / SKILLS_LOCK_PATH).read_text(encoding="utf-8")
        lock = load_skills_lock(lock_text)
    except (OSError, SkillsRecordError) as error:
        print(f"skills checker: {error}", file=sys.stderr)
        return 1
    rows = clone_findings(
        lock,
        options.source,
        options.clone_dir,
        root / SKILLS_ROOT,
    )
    if not rows:
        known = ", ".join(sorted({entry.source for entry in lock.entries}))
        print(
            f"skills checker: no lock entry has source {options.source!r}; "
            f"known sources: {known}",
            file=sys.stderr,
        )
        return 1
    for name, status in rows:
        print(f"{name}: {status}")
    return 0 if all(status == "match" for _name, status in rows) else 1


def walk_files(directory: Path) -> Iterator[tuple[Path, tuple[str, ...]]]:
    """Yield the regular files the hash covers, as (path, relative parts).

    The CLI skips ``.git`` and ``node_modules`` directories.
    """

    def visit(
        current: Path, relative: tuple[str, ...]
    ) -> Iterator[tuple[Path, tuple[str, ...]]]:
        with os.scandir(current) as entries:
            for entry in entries:
                name = entry.name
                path = current / name
                if entry.is_dir(follow_symlinks=False):
                    if name in {".git", "node_modules"}:
                        continue
                    yield from visit(path, relative + (name,))
                elif entry.is_file(follow_symlinks=False):
                    yield path, relative + (name,)

    yield from visit(directory, ())


def folder_hash(directory: str | os.PathLike[str] | Path) -> str:
    """Hash a skill folder exactly as the CLI does.

    Relative POSIX paths use :func:`collation_key`, an approximation of ICU root
    collation.  If a future upstream layout exposes a distinction this key orders
    differently, the lock must remain untouched: refine this key and let the
    tripwire identify the affected hash.
    """

    root = Path(directory)
    files = sorted(
        walk_files(root),
        key=lambda item: collation_key(Path(*item[1]).as_posix()),
    )
    digest = hashlib.sha256()
    for path, relative_parts in files:
        relative = Path(*relative_parts).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _primary_character_key(character: str) -> tuple[int, str | int]:
    if character in _PUNCTUATION_ORDER:
        return (0, _PUNCTUATION_ORDER.index(character))
    if character.isdigit():
        return (1, ord(character))
    if character.isalpha():
        return (2, character.casefold())
    return (0, 8 + ord(character))


def _case_character_key(character: str) -> tuple[int, str]:
    if character.islower():
        case = 0
    elif character.isupper():
        case = 1
    else:
        case = 2
    return case, character


def collation_key(
    path: str,
) -> tuple[
    tuple[tuple[int, str | int], ...],
    tuple[tuple[int, str], ...],
]:
    """Return the skill CLI's ICU-like path ordering key.

    Punctuation sorts before digits and letters in the documented ICU order,
    letters compare case-insensitively, and lowercase wins a full primary tie.
    """

    primary = tuple(_primary_character_key(character) for character in path)
    secondary = tuple(_case_character_key(character) for character in path)
    return primary, secondary


if __name__ == "__main__":
    sys.exit(main())
