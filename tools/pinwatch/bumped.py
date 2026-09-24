"""Generate the release note's pin-change section from lock history."""

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import yaml  # type: ignore[import-untyped]

import gideon
from gideon.host import images, lock, models, upgrade
from gideon.host.sysio import Host, RealHost
from tools.pinwatch import pins, pr, skills

_MINIMUM_VERSION = upgrade.Version.parse("0.2.0")
_TAG = r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
_PIN_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]*"

SINCE_LINE: Final = re.compile(rf"^Since {_TAG}:$")
MOVED_LINE: Final = re.compile(
    rf"^- {_PIN_ID}: (?:.+ → .+|rebuilt at the same version \(the digest moved\)|new at this release \(.+\))$"
)
NONE_LINE: Final = re.compile(rf"^No pin moved since {_TAG}\.$")

_CHECKOUT_FIX = "Inspect the checkout and its reachable tags, then re-run python3 -m tools.pinwatch.bumped."


class BumpedError(ValueError):
    """A release-note pin history cannot be generated safely."""

    def __init__(self, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.problem = problem
        self.fix = fix

    def __str__(self) -> str:
        return f"{self.problem} Fix: {self.fix}"


def _run(host: Host, root: Path, argv: Sequence[str]) -> str:
    result = host.run(argv, cwd=root)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        command = " ".join(argv[:3])
        raise BumpedError(f"{command} exited {result.returncode}: {detail}", _CHECKOUT_FIX)
    return result.stdout


def _tags(host: Host, root: Path) -> tuple[str, ...]:
    output = _run(host, root, ["git", "tag", "--list", "v*", "--merged", "HEAD"])
    return tuple(tag for tag in output.splitlines() if tag)


def _since_tag(
    tags: Sequence[str], requested: str | None
) -> str:
    if requested is not None:
        if requested not in tags:
            raise BumpedError(
                f"requested since tag {requested!r} is not in the tags merged into HEAD",
                _CHECKOUT_FIX,
            )
        try:
            upgrade.Version.from_tag(requested)
        except (TypeError, ValueError) as error:
            raise BumpedError(
                f"requested since tag {requested!r} is not a valid release tag: {error}",
                "Use a reachable tag matching v<major>.<minor>.<patch>, then re-run the generator.",
            ) from None
        return requested

    current = upgrade.Version.parse(gideon.__version__)
    parsed: list[tuple[upgrade.Version, str]] = []
    for tag in tags:
        try:
            parsed.append((upgrade.Version.from_tag(tag), tag))
        except (TypeError, ValueError):
            continue
    below_current = [(version, tag) for version, tag in parsed if version < current]
    eligible = [
        (version, tag) for version, tag in below_current if version >= _MINIMUM_VERSION
    ]
    candidates = eligible or below_current
    if not candidates:
        raise BumpedError(
            f"no reachable release tag is below the checkout version {gideon.__version__}",
            "Create or fetch a reachable release tag below the checkout version, then re-run the generator.",
        )
    return max(candidates)[1]


def _read(host: Host, root: Path, relative: str) -> str:
    try:
        return host.read_text(root / relative)
    except OSError as error:
        raise BumpedError(
            f"cannot read {relative} from checkout {root}: {error}",
            f"Restore {relative} in the checkout, then re-run the generator.",
        ) from error


def _current_records(
    host: Host, root: Path
) -> tuple[
    dict[str, Mapping[str, object]],
    images.ImageLock,
    lock.HostLock,
    models.ModelsLock,
    skills.Provenance,
]:
    image_result = images.load_image_lock_text(_read(host, root, "images.lock"))
    host_result = lock.load_host_lock_text(_read(host, root, "host.lock"))
    models_result = models.load_models_lock_text(_read(host, root, "models.lock"))
    if image_result.errors or image_result.document is None:
        raise BumpedError(
            images.render_errors(image_result.errors) or "images.lock could not be loaded",
            "Correct images.lock in the working tree, then re-run the generator.",
        )
    if host_result.errors or host_result.document is None:
        raise BumpedError(
            lock.render_errors(host_result.errors) or "host.lock could not be loaded",
            "Correct host.lock in the working tree, then re-run the generator.",
        )
    if models_result.errors or models_result.document is None:
        raise BumpedError(
            models.render_errors(models_result.errors) or "models.lock could not be loaded",
            "Correct models.lock in the working tree, then re-run the generator.",
        )
    assert image_result.lock is not None
    assert host_result.lock is not None
    assert models_result.lock is not None
    documents = {
        "images.lock": cast(Mapping[str, object], image_result.document),
        "host.lock": cast(Mapping[str, object], host_result.document),
        "models.lock": cast(Mapping[str, object], models_result.document),
    }
    try:
        provenance = skills.parse_provenance(_read(host, root, skills.PROVENANCE_PATH))
    except skills.SkillsRecordError as error:
        raise BumpedError(error.problem, error.fix) from None
    return (
        documents,
        image_result.lock,
        host_result.lock,
        models_result.lock,
        provenance,
    )


def _old_document(host: Host, root: Path, tag: str, relative: str) -> Mapping[str, object]:
    """The lock at *tag* as a plain YAML document, or an empty one when absent there.

    Existence is read from ``git ls-tree``, which prints nothing for a path the
    tag does not carry, rather than from ``git show``'s message, which differs
    by whether the path exists on disk.
    """

    listed = _run(host, root, ["git", "ls-tree", "--name-only", tag, "--", relative])
    if not listed.strip():
        return {}
    text = _run(host, root, ["git", "show", f"{tag}:{relative}"])
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise BumpedError(
            f"{tag}:{relative} is not valid YAML: {error}",
            f"Correct the {relative} record at {tag}, then re-run the generator.",
        ) from None
    if document is None:
        return {}
    if not isinstance(document, Mapping):
        raise BumpedError(
            f"{tag}:{relative} does not contain a mapping",
            f"Correct the {relative} record at {tag}, then re-run the generator.",
        )
    return cast(Mapping[str, object], document)


_MISSING = object()


def _at(document: Mapping[str, object], path: str) -> object:
    current: object = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _value_changed(old: object, new: object) -> bool:
    if old is _MISSING or new is _MISSING:
        return True
    if isinstance(old, Mapping) or isinstance(new, Mapping):
        return old != new
    return str(old) != str(new)


_REVISION_SHOWN = 12


def _token(path: str, value: object) -> str | None:
    """The version token a lock value carries, a revision shortened for the note."""

    if value is _MISSING:
        return None
    token = pr.version_token(path, str(value))
    if token is not None and path.endswith(".revision"):
        return token[:_REVISION_SHOWN]
    return token


def _moved_line(
    pin: pins.Pin,
    old_document: Mapping[str, object],
    new_document: Mapping[str, object],
) -> str | None:
    old_values = {path: _at(old_document, path) for path in pin.note_paths}
    new_values = {path: _at(new_document, path) for path in pin.note_paths}
    if not any(
        _value_changed(old_values[path], new_values[path]) for path in pin.note_paths
    ):
        return None
    if any(value is _MISSING for value in old_values.values()):
        new_token = next(
            (
                token
                for path in pin.note_paths
                if (token := _token(path, new_values[path])) is not None
            ),
            None,
        )
        return f"- {pin.id}: new at this release ({new_token or 'unversioned'})"
    for path in pin.note_paths:
        old_token = _token(path, old_values[path])
        new_token = _token(path, new_values[path])
        if new_token is not None and old_token != new_token:
            return f"- {pin.id}: {old_token} → {new_token}"
    return f"- {pin.id}: rebuilt at the same version (the digest moved)"


def render(
    since: str,
    registry: Sequence[pins.Pin],
    old_documents: Mapping[str, Mapping[str, object]],
    new_documents: Mapping[str, Mapping[str, object]],
) -> str:
    """Render the generated release-note section."""

    moved: list[str] = []
    for pin in registry:
        if pin.lock not in pr.PRODUCT_LOCKS:
            continue
        line = _moved_line(
            pin,
            old_documents.get(pin.lock, {}),
            new_documents[pin.lock],
        )
        if line is not None:
            moved.append(line)
    if not moved:
        return f"No pin moved since {since}.\n"
    return "\n".join((f"Since {since}:", *moved)) + "\n"


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    root: Path | None = None,
) -> int:
    """Generate the release-note pin-change section."""

    parser = argparse.ArgumentParser(prog="python3 -m tools.pinwatch.bumped")
    parser.add_argument("--since")
    parser.add_argument("--root", type=Path)
    options = parser.parse_args(argv)
    io = host or RealHost()
    checkout = root or options.root or Path(__file__).resolve().parents[2]
    try:
        since = _since_tag(_tags(io, checkout), options.since)
        (
            new_documents,
            image_lock,
            host_lock,
            models_lock,
            provenance,
        ) = _current_records(io, checkout)
        registry = pins.pin_registry(
            image_lock,
            host_lock,
            models_lock,
            provenance=provenance,
        )
        old_documents = {
            relative: _old_document(io, checkout, since, relative)
            for relative in ("images.lock", "host.lock", "models.lock")
        }
        print(render(since, registry, old_documents, new_documents), end="")
    except BumpedError as error:
        print(f"pin watch: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
