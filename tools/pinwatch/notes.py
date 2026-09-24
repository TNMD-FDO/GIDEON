"""Validate research-note pin bindings against the pin-watch vocabulary.

Every research note opens with YAML front matter naming the pins
it was verified against and the version of each — the author's claim, never
compared to a lock. This module parses that record through the ``Host`` seam,
derives the pin vocabulary from the checkout's records (the pin watch's
registry and ``requirements-dev.txt`` as ``dev.<package>``), joins the two for
the bump body's re-verification list, and lists the join per pin behind
``python3 -m tools.pinwatch.notes``.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml  # type: ignore[import-untyped]

from gideon.host import images, lock, models
from gideon.host.sysio import Host, RealHost
from tools.pinwatch import skills
from tools.pinwatch.paths import PROVENANCE_PATH, RESEARCH_NOTES_PATH
from tools.pinwatch.pins import Pin, pin_registry

NoteNamespace = Literal["watched", "dev"]

_FRONT_MATTER_FIX: Final = (
    "Write front matter with only verified_against, a non-empty list, and one "
    "pin/version mapping per verified pin."
)
_VOCABULARY_FIX: Final = (
    "Use a pin from images.lock, host.lock, models.lock, or requirements-dev.txt."
)
_REQUIREMENT: Final = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)\s*==\s*(?P<version>[^\s#]+)\s*$"
)


class NotesError(ValueError):
    """A research-note record is invalid."""

    def __init__(self, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.fix = fix

    def __str__(self) -> str:
        return f"{self.problem} Fix: {self.fix}"


@dataclass(frozen=True, slots=True)
class NoteBinding:
    """One research note's verification claim."""

    note: str
    pin: str
    version: str


@dataclass(frozen=True, slots=True)
class VocabularyEntry:
    """One known pin id and its namespace."""

    pin: str
    namespace: NoteNamespace


@dataclass(frozen=True, slots=True)
class NoteVocabulary:
    """The ordered pin vocabulary."""

    entries: tuple[VocabularyEntry, ...]

    def namespace(self, pin_id: str) -> NoteNamespace | None:
        """Return the namespace for *pin_id*."""

        for entry in self.entries:
            if entry.pin == pin_id:
                return entry.namespace
        return None


class _DuplicateKeyError(yaml.YAMLError):
    """Raised when front matter repeats a mapping key."""


class NotesLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate front-matter keys."""


NotesLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: NotesLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 2
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


NotesLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


def _front_matter_error(note: str, problem: str) -> NotesError:
    return NotesError(f"{note} {problem}", _FRONT_MATTER_FIX)


def parse_front_matter(text: str, note: str) -> tuple[NoteBinding, ...]:
    """Parse one note's front matter."""

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise _front_matter_error(note, "must open with --- on line 1")
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise _front_matter_error(note, "has no closing --- line") from error

    block = "\n".join(lines[1:closing])
    try:
        document = yaml.load(block, Loader=NotesLoader)
    except _DuplicateKeyError as error:
        raise _front_matter_error(note, f"has {error}") from error
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 2}" if isinstance(line, int) else ""
        raise _front_matter_error(note, f"has a YAML parse error{location}: {error}") from error

    if not isinstance(document, dict):
        raise _front_matter_error(note, "must be a mapping")
    if set(document) != {"verified_against"}:
        raise _front_matter_error(note, "must contain only the verified_against key")
    raw_bindings = document["verified_against"]
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise _front_matter_error(note, "verified_against must be a non-empty list")

    bindings: list[NoteBinding] = []
    for index, raw_binding in enumerate(raw_bindings, start=1):
        if not isinstance(raw_binding, dict):
            raise _front_matter_error(
                note, f"verified_against item {index} must be a mapping"
            )
        if set(raw_binding) != {"pin", "version"}:
            raise _front_matter_error(
                note,
                f"verified_against item {index} must contain only pin and version",
            )
        pin = raw_binding["pin"]
        version = raw_binding["version"]
        if not isinstance(pin, str) or not pin.strip():
            raise _front_matter_error(
                note,
                f"verified_against item {index} requires a non-empty string pin",
            )
        if not isinstance(version, str) or not version.strip():
            raise _front_matter_error(
                note,
                f"verified_against item {index} requires a non-empty string version",
            )
        bindings.append(NoteBinding(note, pin, version))
    return tuple(bindings)


def read_bindings(host: Host, root: Path) -> tuple[NoteBinding, ...]:
    """Read sorted research notes through the Host seam."""

    directory = root / RESEARCH_NOTES_PATH
    try:
        names = sorted(name for name in host.listdir(directory) if name.endswith(".md"))
    except OSError as error:
        raise NotesError(
            f"cannot list research notes in checkout {root}: {error}",
            f"Restore the {RESEARCH_NOTES_PATH} directory in the checkout and retry.",
        ) from error

    bindings: list[NoteBinding] = []
    for name in names:
        relative = Path(RESEARCH_NOTES_PATH) / name
        note = relative.as_posix()
        try:
            text = host.read_text(root / relative)
        except OSError as error:
            raise NotesError(
                f"cannot read {note} from checkout {root}: {error}",
                "Restore the note and its front matter, then retry.",
            ) from error
        bindings.extend(parse_front_matter(text, note))
    return tuple(bindings)


def build_vocabulary(pins: Sequence[Pin], requirements_text: str) -> NoteVocabulary:
    """Derive the ordered pin vocabulary from loaded records."""

    entries: list[VocabularyEntry] = []
    known: set[str] = set()

    def add(pin_id: str, namespace: NoteNamespace) -> None:
        if pin_id not in known:
            entries.append(VocabularyEntry(pin_id, namespace))
            known.add(pin_id)

    for pin in pins:
        add(pin.id, "watched")

    for line in requirements_text.splitlines():
        match = _REQUIREMENT.fullmatch(line)
        if match is not None:
            add(f"dev.{match.group('name').lower()}", "dev")
    return NoteVocabulary(tuple(entries))


def validate_bindings(
    bindings: Sequence[NoteBinding], vocabulary: NoteVocabulary
) -> None:
    """Refuse bindings outside the derived vocabulary."""

    for binding in bindings:
        if vocabulary.namespace(binding.pin) is None:
            raise NotesError(
                f"{binding.note} names unknown pin {binding.pin!r}",
                _VOCABULARY_FIX,
            )


def bound_to(
    bindings: Sequence[NoteBinding], pin_id: str
) -> tuple[NoteBinding, ...]:
    """Return bindings for *pin_id* in note-reading order."""

    return tuple(binding for binding in bindings if binding.pin == pin_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.pinwatch.notes")
    parser.add_argument("--root", type=Path)
    return parser


def _read_file(host: Host, root: Path, relative: str) -> str:
    try:
        return host.read_text(root / relative)
    except OSError as error:
        raise NotesError(
            f"cannot read {relative} from checkout {root}: {error}",
            "Restore the named record in the checkout and retry.",
        ) from error


def _print_notes_error(error: NotesError) -> None:
    print(f"pin watch: {error.problem} Fix: {error.fix}", file=sys.stderr)


def _print_rows(
    bindings: Sequence[NoteBinding], vocabulary: NoteVocabulary
) -> None:
    for entry in vocabulary.entries:
        related = bound_to(bindings, entry.pin)
        label = entry.pin
        if entry.namespace != "watched":
            label += " (recorded, not watched)"
        if not related:
            print(f"{label}: none")
            continue
        details = ", ".join(
            f"{binding.note} ({binding.version})" for binding in related
        )
        print(f"{label}: {len(related)} — {details}")


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    root: Path | None = None,
) -> int:
    """List note bindings against loaded records."""

    options = _parser().parse_args(argv)
    io = host or RealHost()
    checkout = root or options.root or Path(__file__).resolve().parents[2]
    try:
        image_result = images.load_image_lock_text(_read_file(io, checkout, "images.lock"))
        host_result = lock.load_host_lock_text(_read_file(io, checkout, "host.lock"))
        models_result = models.load_models_lock_text(_read_file(io, checkout, "models.lock"))
        tooling_text = _read_file(io, checkout, PROVENANCE_PATH)
        requirements_text = _read_file(io, checkout, "requirements-dev.txt")
    except NotesError as error:
        _print_notes_error(error)
        return 1

    if image_result.errors:
        print(images.render_errors(image_result.errors), file=sys.stderr)
    if host_result.errors:
        print(lock.render_errors(host_result.errors), file=sys.stderr)
    if models_result.errors:
        print(models.render_errors(models_result.errors), file=sys.stderr)
    if (
        image_result.lock is None
        or host_result.lock is None
        or models_result.lock is None
    ):
        return 1

    try:
        provenance = skills.parse_provenance(tooling_text)
    except skills.SkillsRecordError as error:
        print(f"pin watch: {error.problem} Fix: {error.fix}", file=sys.stderr)
        return 1

    pins = pin_registry(
        image_result.lock,
        host_result.lock,
        models_result.lock,
        provenance,
    )
    try:
        vocabulary = build_vocabulary(pins, requirements_text)
        bindings = read_bindings(io, checkout)
        validate_bindings(bindings, vocabulary)
    except NotesError as error:
        _print_notes_error(error)
        return 1

    _print_rows(bindings, vocabulary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
