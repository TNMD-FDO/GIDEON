"""Comment-preserving, loader-validated edits to pin-watch records."""

import copy
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from gideon.host import models
from gideon.host.images import load_image_lock_text
from gideon.host.images import render_errors as render_image_errors
from gideon.host.lock import load_host_lock_text
from gideon.host.lock import render_errors as render_host_errors
from tools.pinwatch import hub
from tools.pinwatch.pins import Bump
from tools.pinwatch.skills import SkillsRecordError, patch_provenance


class PatchError(ValueError):
    """A lock edit could not be anchored or validated safely."""


# The skill record is patched by its own module; the YAML path below is the
# product locks'.
_RECORD_PATCHERS = {"docs/agents/tooling.md": patch_provenance}


_KEY = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9][A-Za-z0-9_.-]*):(?P<rest>.*)$")
_INTEGER = re.compile(r"^-?\d+$")


def _comment_split(value: str) -> tuple[str, str]:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif quote is not None and character == "\\":
            escaped = True
        elif quote is not None and character == quote:
            quote = None
        elif quote is None and character in "\"'":
            quote = character
        elif (
            quote is None
            and character == "#"
            and (index == 0 or value[index - 1].isspace())
        ):
            return value[:index], value[index:]
    return value, ""


def _render_scalar(old: str, new: str) -> str:
    stripped = old.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        quote = stripped[0]
        escaped = new.replace("\\", "\\\\").replace(quote, f"\\{quote}")
        return f"{quote}{escaped}{quote}"
    return new


@dataclass(frozen=True, slots=True)
class _KeyLine:
    """One mapping key line of a lock text, with its dotted path."""

    number: int
    match: re.Match[str]
    path: tuple[str, ...]
    value_part: str
    comment: str
    newline: str

    @property
    def indent(self) -> int:
        return len(self.match.group("indent"))

    @property
    def is_block(self) -> bool:
        return not self.value_part.strip()


def _key_lines(lines: Sequence[str]) -> Iterator[_KeyLine]:
    """Walk every mapping key of *lines* with its dotted path."""

    stack: list[tuple[int, str]] = []
    for number, line in enumerate(lines):
        content = line.rstrip("\r\n")
        match = _KEY.fullmatch(content)
        if match is None:
            continue
        indent = len(match.group("indent"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = tuple(key for _, key in stack) + (match.group("key"),)
        value_part, comment = _comment_split(match.group("rest"))
        yield _KeyLine(number, match, path, value_part, comment, line[len(content) :])
        if not value_part.strip():
            stack.append((indent, match.group("key")))


def _one_key(lines: Sequence[str], key_path: str, *, block: bool) -> _KeyLine:
    """The one key line at *key_path*: a block when *block*, a scalar otherwise."""

    target = tuple(key_path.split("."))
    matches = [key for key in _key_lines(lines) if key.path == target]
    if len(matches) != 1:
        reason = "absent" if not matches else "ambiguous"
        raise PatchError(f"key path is {reason}: {key_path}")
    key = matches[0]
    if key.is_block != block:
        kind = "block" if block else "scalar"
        raise PatchError(f"key path is not a {kind}: {key_path}")
    return key


def replace_scalar(text: str, key_path: str, new_value: str) -> str:
    """Replace one dotted mapping scalar while preserving all other bytes."""

    if not key_path or "\n" in new_value or "\r" in new_value:
        raise PatchError(f"invalid scalar replacement for {key_path!r}")
    lines = text.splitlines(keepends=True)
    key = _one_key(lines, key_path, block=False)
    value_part = key.value_part
    leading = value_part[: len(value_part) - len(value_part.lstrip())]
    trailing = value_part[len(value_part.rstrip()) :]
    replacement = (
        key.match.group("indent")
        + key.match.group("key")
        + ":"
        + leading
        + _render_scalar(value_part, new_value)
        + trailing
        + key.comment
        + key.newline
    )
    return "".join((*lines[: key.number], replacement, *lines[key.number + 1 :]))


def replace_block(text: str, key_path: str, lines: Sequence[str]) -> str:
    """Replace one mapping key's indented block while preserving all other bytes.

    *lines* are the block's new lines relative to its own indentation (the
    entries at column zero); each is indented two spaces past the key. The old
    block is every following line that is blank, a comment, or indented deeper
    than the key, up to the first line at the key's indentation or shallower.
    """

    source_lines = text.splitlines(keepends=True)
    key = _one_key(source_lines, key_path, block=True)
    end = key.number + 1
    while end < len(source_lines):
        content = source_lines[end].rstrip("\r\n")
        stripped = content.lstrip(" ")
        indent = len(content) - len(stripped)
        if content.strip() and not stripped.startswith("#") and indent <= key.indent:
            break
        end += 1
    prefix = " " * (key.indent + 2)
    replacement = "".join(f"{prefix}{line}{key.newline}" for line in lines)
    return "".join((*source_lines[: key.number + 1], replacement, *source_lines[end:]))


def _load(text: str, lock: str) -> Mapping[str, object]:
    """The loaded document, through the product's own loader for *lock*."""

    if lock == "images.lock":
        image_result = load_image_lock_text(text)
        if image_result.errors or image_result.document is None:
            raise PatchError(render_image_errors(image_result.errors))
        return image_result.document
    if lock == "host.lock":
        host_result = load_host_lock_text(text)
        if host_result.errors or host_result.document is None:
            raise PatchError(render_host_errors(host_result.errors))
        return host_result.document
    if lock == "models.lock":
        models_result = models.load_models_lock_text(text)
        if models_result.errors or models_result.document is None:
            raise PatchError(models.render_errors(models_result.errors))
        return models_result.document
    raise PatchError(f"unknown lock file: {lock}")


def _lookup(document: Mapping[str, object], path: str) -> object:
    current: object = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            raise PatchError(f"key path is absent from loaded document: {path}")
        current = current[segment]
    return current


def _set(document: dict[str, Any], path: str, value: object) -> None:
    segments = path.split(".")
    current: dict[str, Any] = document
    for segment in segments[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            raise PatchError(f"key path is absent from loaded document: {path}")
        current = child
    if segments[-1] not in current:
        raise PatchError(f"key path is absent from loaded document: {path}")
    current[segments[-1]] = value


def _typed_new(old: object, new: str) -> object:
    if isinstance(old, int) and not isinstance(old, bool) and _INTEGER.fullmatch(new):
        return int(new)
    return new


def apply_bump(text: str, bump: Bump) -> str:
    """Apply and validate a bump, refusing any unintended loaded-value change."""

    patcher = _RECORD_PATCHERS.get(bump.lock)
    if patcher is not None:
        try:
            return patcher(text, bump.changes)
        except SkillsRecordError as error:
            raise PatchError(str(error)) from error

    original = _load(text, bump.lock)
    seen: set[str] = set()
    expected = copy.deepcopy(dict(original))
    for change in bump.changes:
        if change.key_path in seen:
            raise PatchError(f"bump contains a duplicate key path: {change.key_path}")
        seen.add(change.key_path)
        old = _lookup(original, change.key_path)
        if str(old) != change.old:
            raise PatchError(
                f"bump is anchored to {change.key_path}={change.old!r}, "
                f"but the lock contains {old!r}"
            )
        _set(expected, change.key_path, _typed_new(old, change.new))
    for block in bump.blocks:
        if block.key_path in seen:
            raise PatchError(f"bump contains a duplicate key path: {block.key_path}")
        seen.add(block.key_path)
        old = _lookup(original, block.key_path)
        if not isinstance(old, Mapping):
            raise PatchError(f"bump block path is not a mapping: {block.key_path}")
        if old != block.old:
            raise PatchError(
                f"bump is anchored to {block.key_path}={block.old!r}, "
                f"but the lock contains {old!r}"
            )
        _set(expected, block.key_path, copy.deepcopy(dict(block.new)))

    patched = text
    for change in bump.changes:
        patched = replace_scalar(patched, change.key_path, change.new)
    for block in bump.blocks:
        patched = replace_block(
            patched,
            block.key_path,
            hub.render_files_block(0, cast(Mapping[str, Mapping[str, object]], block.new)),
        )
    loaded = _load(patched, bump.lock)
    if loaded != expected:
        raise PatchError("patched lock changed values outside the bump's key paths")
    return patched
