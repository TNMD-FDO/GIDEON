"""The forward release upgrade command.

Forward upgrades and rollbacks run the engine verify child after structural
verify and before their applied audit row; a failure records the failed phase.
"""

import argparse
import contextlib
import json
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from functools import total_ordering
from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]

import gideon
from gideon.host import audit as audit_module
from gideon.host import backup, backupset, preflight, site, stack
from gideon.host.render.engine import ENGINE_SERVICE_NAME
from gideon.host.report import StageResult, command_detail, print_stage, refusal
from gideon.host.stages import aware_now, site_problem
from gideon.host.steps.site_dirs import AGE_IDENTITY_PATH
from gideon.host.sysio import Host, LockingHost, PathLike, RealHost

_SITE_PATH: Final = "/etc/gideon/site.yaml"
_RENDERED_DIR: Final = "/etc/gideon/rendered"
_UPGRADE: Final = "sudo python3 -m gideon upgrade"
_ROLLBACK: Final = f"{_UPGRADE} --rollback"
_TAG_FIX: Final = f"Provide a release tag such as v1.2.3, then retry {_UPGRADE} <tag>."
_TAG_GRAMMAR_FIX: Final = (
    "Use a tag matching v<major>.<minor>.<patch> with an optional pre-release suffix, "
    "then retry."
)
_VERSION_RE: Final = re.compile(
    r"^(?P<prefix>v?)(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_VERSION_ASSIGNMENT_RE: Final = re.compile(
    r"^\s*__version__\s*=\s*([\"'])([^\"']+)\1\s*$", re.MULTILINE
)
_HEADING_RE: Final = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_NO_BREAKING: Final = "Breaking: the release notes name none."
_PRE_RELEASE_SET_SUFFIX: Final = re.compile(r"-\d{8}T\d{6}Z$")

# The next step depends on where the run failed: before the checkout
# nothing has moved; at the new tree's provision or preflight the product is still
# the previous release; at or after its apply only a rollback goes back.
_BEFORE_NEW_TREE_FIX: Final = (
    "Correct the refusal, then re-run sudo python3 -m gideon upgrade {tag}."
)
_AFTER_CHECKOUT_FIX: Final = (
    "Reboot if asked or correct the refusal, then re-run sudo python3 -m gideon upgrade "
    "{tag}; to abandon, sudo python3 -m gideon upgrade --rollback moves the checkout back."
)
_AFTER_APPLY_FIX: Final = "Run sudo python3 -m gideon upgrade --rollback."
# Rollback's next step is always the same command; only the by-hand cases differ.
_ROLLBACK_FIX: Final = f"Correct the refusal, then re-run {_ROLLBACK}."
_ROLLBACK_FETCH_FIX: Final = (
    f"Run git fetch --tags origin in the checkout as its owner, then re-run {_ROLLBACK}."
)
_ROLLBACK_BY_HAND_FIX: Final = (
    "Go back by hand: sudo python3 -m gideon restore --from staging --set <label> of an "
    "earlier set, then sudo python3 -m gideon apply."
)
_ROLLBACK_SAFETY_FIX: Final = (
    "Correct backup run's refusal above, or stop the stack with docker compose -f "
    "/etc/gideon/rendered/compose.yaml down to roll back without a safety set, then "
    f"re-run {_ROLLBACK}."
)

Runner = Callable[[argparse.Namespace], int]


@total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    """A SemVer 2.0 version without build metadata."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "Version":
        """Parse a version string with an optional ``v`` tag prefix."""

        if not isinstance(value, str):
            raise TypeError("version must be a string")
        match = _VERSION_RE.fullmatch(value)
        if match is None:
            raise ValueError(f"invalid SemVer version: {value}")
        prerelease_text = match.group("pre")
        prerelease = () if prerelease_text is None else tuple(prerelease_text.split("."))
        for identifier in prerelease:
            if identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0":
                raise ValueError(
                    f"numeric pre-release identifier has leading zero: {identifier}"
                )
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            prerelease,
        )

    @classmethod
    def from_tag(cls, value: str) -> "Version":
        """Parse a release tag, which must carry the ``v`` prefix."""

        if not isinstance(value, str) or not value.startswith("v"):
            raise ValueError("release tag must start with v")
        return cls.parse(value)

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core if not self.prerelease else f"{core}-{'.'.join(self.prerelease)}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if core != other_core:
            return core < other_core
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease, strict=False):
            if left == right:
                continue
            left_numeric = left.isdigit()
            right_numeric = right.isdigit()
            if left_numeric and right_numeric:
                return int(left) < int(right)
            if left_numeric != right_numeric:
                return left_numeric
            return left < right
        return len(self.prerelease) < len(other.prerelease)


@dataclass(frozen=True, slots=True)
class UpgradePlan:
    """Facts judged before checkout and consumed by all later stages."""

    checkout: Path
    owner: str
    owner_uid: int
    current_version: Version
    from_commit: str
    tag: str
    target_version: Version
    target_commit: str
    set_label: str


@dataclass(frozen=True, slots=True)
class _CheckoutOwner:
    name: str
    uid: int


@dataclass(frozen=True, slots=True)
class _RollbackPlan:
    """Facts selected before rollback mutates the checkout or the stack."""

    checkout: Path
    owner: _CheckoutOwner
    current_version: Version
    target_version: Version
    target_release: str
    target_commit: str
    set_label: str
    archive_through: datetime
    restore_needed: bool = False


# A rollback in progress, beside push.json in the staging directory: outside every
# restore root and every backup root, so a restore cannot overwrite it.  Written
# before the first irreversible stage, updated when the restore is done, removed
# once verify passes.  A re-run cannot otherwise tell a finished rollback from one
# that stopped after its restore's files stage, which leaves the rendered manifest
# and the applied record both naming the set's release.
_MARKER_PATH: Final = Path(backupset.STAGING) / "rollback.json"
_MARKER_FIX: Final = (
    f"Check {_MARKER_PATH}; remove it when the rollback it names is over, then re-run {_ROLLBACK}."
)


@dataclass(frozen=True, slots=True)
class _Marker:
    set_label: str
    restore_needed: bool
    restored: bool
    started: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True) + "\n"


def _read_marker(io: Host) -> "_Marker | None | str":
    """The rollback in progress, None when there is none, or the problem with the file."""

    try:
        text = io.read_text(_MARKER_PATH)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        return f"cannot read {_MARKER_PATH}: {exc}"
    try:
        document = json.loads(text)
    except ValueError as exc:
        return f"{_MARKER_PATH} is malformed: {exc}"
    if not isinstance(document, Mapping) or set(document) != {
        "set_label", "restore_needed", "restored", "started"
    }:
        return f"{_MARKER_PATH} is malformed: it must carry exactly the four rollback fields"
    set_label, restore_needed, restored, started = (
        document["set_label"], document["restore_needed"], document["restored"], document["started"]
    )
    if not isinstance(restore_needed, bool) or not isinstance(restored, bool):
        return f"{_MARKER_PATH} is malformed: restore_needed and restored must be true or false"
    if restored and not restore_needed:
        return f"{_MARKER_PATH} is malformed: restored without a restore needed"
    if not isinstance(set_label, str) or _release_tag_for_set(set_label) is None:
        return f"{_MARKER_PATH} is malformed: set_label is not a pre-upgrade set label"
    if not isinstance(started, str):
        return f"{_MARKER_PATH} is malformed: started must be a string"
    try:
        datetime.fromisoformat(started)
    except ValueError:
        return f"{_MARKER_PATH} is malformed: started is not an ISO 8601 instant"
    return _Marker(set_label, restore_needed, restored, started)


def _write_marker(io: Host, marker: _Marker) -> str | None:
    try:
        io.write_text(_MARKER_PATH, marker.to_json())
    except OSError as exc:
        return f"cannot write {_MARKER_PATH}: {exc}"
    return None


class _StageTimer:
    """Collect durations for one ordered host command."""

    def __init__(self) -> None:
        self.durations: dict[str, float] = {}

    @contextlib.contextmanager
    def timed(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.durations[name] = round(time.monotonic() - started, 3)


def _refuse(problem: str, fix: str) -> int:
    print(refusal("upgrade", problem, fix), file=sys.stderr)
    return 1


def _run_git(
    io: Host,
    checkout: Path,
    owner: _CheckoutOwner,
    arguments: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    argv = ["git", *arguments]
    if owner.uid != 0:
        argv = ["sudo", "-u", owner.name, *argv]
    return io.run(argv, cwd=checkout)


def _owner(io: Host, checkout: Path) -> _CheckoutOwner | None:
    try:
        uid = io.stat(checkout).st_uid
        result = io.run(["getent", "passwd", str(uid)])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if len(fields) < 7 or not fields[0]:
            continue
        try:
            entry_uid = int(fields[2])
        except ValueError:
            continue
        if entry_uid == uid:
            return _CheckoutOwner(fields[0], uid)
    return None


def _preconditions(
    io: Host, *, checkout: Path, retry: str
) -> tuple[StageResult, _CheckoutOwner | None]:
    """Docker Compose answers and the checkout is a clean work tree with a known owner."""

    docker_fix = f"Run sudo python3 -m gideon host provision --only docker-engine, then re-run {retry}."
    owner_fix = f"Correct the checkout's ownership and its passwd entry, then re-run {retry}."
    worktree_fix = f"Commit or stash the checkout's changes as its owner, then re-run {retry}."
    try:
        compose = io.run(["docker", "compose", "version"])
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("preconditions", False, f"Docker Compose is unavailable: {exc}", docker_fix),
            None,
        )
    if compose.returncode != 0:
        return (
            StageResult(
                "preconditions",
                False,
                f"Docker Compose version check failed: {command_detail(compose)}",
                docker_fix,
            ),
            None,
        )

    owner = _owner(io, checkout)
    if owner is None:
        return (
            StageResult("preconditions", False, "checkout owner could not be resolved", owner_fix),
            None,
        )
    try:
        work_tree = _run_git(io, checkout, owner, ("rev-parse", "--is-inside-work-tree"))
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("preconditions", False, f"checkout is not a Git work tree: {exc}", worktree_fix),
            owner,
        )
    if work_tree.returncode != 0 or work_tree.stdout.strip() != "true":
        return (
            StageResult("preconditions", False, "checkout is not a Git work tree", worktree_fix),
            owner,
        )
    try:
        # Untracked files — an operator's transcript being written by tee into
        # the checkout — are no obstacle to a checkout; only changes to tracked
        # files could be carried or lost.
        status = _run_git(io, checkout, owner, ("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("preconditions", False, f"checkout status failed: {exc}", worktree_fix),
            owner,
        )
    if status.returncode != 0:
        return (
            StageResult("preconditions", False, f"checkout status failed: {command_detail(status)}", worktree_fix),
            owner,
        )
    if status.stdout.strip():
        return (
            StageResult(
                "preconditions",
                False,
                "checkout is dirty",
                f"Commit or stash the checkout's changes as {owner.name}, then re-run {retry}.",
            ),
            owner,
        )
    return StageResult("preconditions", True, f"checkout is clean and owned by {owner.name}", ""), owner


def _resolve_tag(
    io: Host, checkout: Path, owner: _CheckoutOwner, tag: str
) -> tuple[str | None, str | None]:
    try:
        result = _run_git(io, checkout, owner, ("rev-parse", f"{tag}^{{commit}}"))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, command_detail(result)
    commit = next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)
    return commit, None if commit is not None else "git rev-parse returned no commit"


def _manual_fetch_fix(checkout: Path, owner: _CheckoutOwner, tag: str) -> str:
    return (
        f"Run git fetch --tags origin in {checkout} as {owner.name}, then re-run "
        f"sudo python3 -m gideon upgrade {tag}."
    )


def _fetch_stage(
    io: Host,
    *,
    checkout: Path,
    owner: _CheckoutOwner,
    tag: str,
) -> tuple[StageResult, str | None, str | None]:
    try:
        fetched = _run_git(io, checkout, owner, ("fetch", "--tags", "origin"))
    except (OSError, subprocess.SubprocessError) as exc:
        fetched = subprocess.CompletedProcess([], 1, "", str(exc))
    target_commit, resolve_problem = _resolve_tag(io, checkout, owner, tag)
    if target_commit is None:
        if fetched.returncode != 0:
            return (
                StageResult(
                    "fetch",
                    False,
                    f"fetch failed and tag {tag} is not present",
                    _manual_fetch_fix(checkout, owner, tag),
                ),
                None,
                None,
            )
        return (
            StageResult(
                "fetch",
                False,
                f"tag {tag} could not be resolved: {resolve_problem or 'unknown error'}",
                _manual_fetch_fix(checkout, owner, tag),
            ),
            None,
            None,
        )
    try:
        head = _run_git(io, checkout, owner, ("rev-parse", "HEAD"))
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            StageResult("fetch", False, f"current commit could not be read: {exc}", _BEFORE_NEW_TREE_FIX.format(tag=tag)),
            None,
            None,
        )
    if head.returncode != 0:
        return (
            StageResult(
                "fetch",
                False,
                f"current commit could not be read: {command_detail(head)}",
                _BEFORE_NEW_TREE_FIX.format(tag=tag),
            ),
            None,
            None,
        )
    from_commit = next((line.strip() for line in head.stdout.splitlines() if line.strip()), None)
    if from_commit is None:
        return (
            StageResult("fetch", False, "current commit could not be read", _BEFORE_NEW_TREE_FIX.format(tag=tag)),
            None,
            None,
        )
    if fetched.returncode != 0:
        detail = f"fetch failed; tag {tag} was already present at {target_commit}, continuing"
    else:
        detail = f"fetched tags; {tag} resolves to {target_commit}"
    return StageResult("fetch", True, detail, ""), target_commit, from_commit


def _tree_version(
    io: Host, checkout: Path, owner: _CheckoutOwner, tag: str
) -> tuple[Version | None, str | None]:
    try:
        result = _run_git(io, checkout, owner, ("show", f"{tag}:gideon/__init__.py"))
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, command_detail(result)
    match = _VERSION_ASSIGNMENT_RE.search(result.stdout)
    if match is None:
        return None, "tag tree does not declare __version__"
    try:
        return Version.parse(match.group(2)), None
    except ValueError as exc:
        return None, str(exc)


def _breaking_text(
    io: Host,
    *,
    checkout: Path,
    owner: _CheckoutOwner,
    current: Version,
    tag: str,
    version: Version,
) -> str:
    """The ``## Breaking`` sections of the release notes the cross passes.

    Each crossed major's ``v<K>.0.0`` note, then the target's own when it is a
    later minor or patch, read from the target tag's tree in version order.
    There is no changelog fallback: a lower tag refuses before this reader, so
    every target it sees is ``v1.0.0`` or later, whose major note the release
    note contract requires; the changelog is internal and not exported.
    """
    note_versions = [
        Version(major, 0, 0)
        for major in range(current.major + 1, version.major + 1)
    ]
    if version.minor != 0 or version.patch != 0:
        note_versions.append(version)
    sections: list[str] = []
    for note_version in note_versions:
        try:
            shown = _run_git(
                io,
                checkout,
                owner,
                ("show", f"{tag}:docs/release-notes/v{note_version}.md"),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if shown.returncode != 0:
            continue
        lines = shown.stdout.splitlines()
        start = next((index for index, line in enumerate(lines) if line.strip() == "## Breaking"), None)
        if start is None:
            continue
        end = next(
            (index for index in range(start + 1, len(lines)) if _HEADING_RE.match(lines[index])),
            len(lines),
        )
        section = "\n".join(lines[start:end]).strip()
        if section:
            sections.append(section)

    return "\n\n".join(sections) or _NO_BREAKING


def _version_stage(
    io: Host,
    *,
    checkout: Path,
    owner: _CheckoutOwner,
    tag: str,
    target_commit: str,
    acknowledge_breaking: bool,
) -> tuple[StageResult, Version | None, Version | None]:
    try:
        current = Version.parse(gideon.__version__)
        target_from_tag = Version.from_tag(tag)
    except ValueError as exc:
        return StageResult("version", False, str(exc), _TAG_FIX), None, None
    target_from_tree, tree_problem = _tree_version(io, checkout, owner, tag)
    if target_from_tree is None:
        return (
            StageResult(
                "version",
                False,
                f"could not read the version from {tag}'s tree: {tree_problem or 'unknown error'}",
                _BEFORE_NEW_TREE_FIX.format(tag=tag),
            ),
            None,
            None,
        )
    if target_from_tree != target_from_tag:
        return (
            StageResult(
                "version",
                False,
                f"tag {tag} names a tree at version {target_from_tree}, not {target_from_tag}",
                f"Retag a tree whose __version__ is {target_from_tag}, then re-run upgrade {tag}.",
            ),
            current,
            target_from_tag,
        )
    if target_from_tag < current:
        return (
            StageResult(
                "version",
                False,
                f"target {tag} is lower than the current version {current}",
                "Use sudo python3 -m gideon upgrade --rollback for a release rollback, "
                "or sudo python3 -m gideon restore --from staging for data restore.",
            ),
            current,
            target_from_tag,
        )
    if target_from_tag.major != current.major:
        # The release notes are the one reader of a major's Breaking section
        # (see _breaking_text for why the changelog is not a second).
        print(
            _breaking_text(
                io,
                checkout=checkout,
                owner=owner,
                current=current,
                tag=tag,
                version=target_from_tag,
            )
        )
        if not acknowledge_breaking:
            return (
                StageResult(
                    "version",
                    False,
                    f"major-version change {current.major} → {target_from_tag.major} is not acknowledged",
                    f"Review the Breaking section, then re-run upgrade {tag} --acknowledge-breaking.",
                ),
                current,
                target_from_tag,
            )
    return (
        StageResult(
            "version",
            True,
            f"current {current}; target {target_from_tag} at {target_commit}",
            "",
        ),
        current,
        target_from_tag,
    )


def _default_runners(
    io: LockingHost, *, site_path: PathLike, rendered_dir: PathLike
) -> dict[str, Runner]:
    """The current tree's in-process commands with the keyword arguments they take."""

    return {
        "preflight": lambda child: preflight.run_preflight(
            child, host=io, site_path=site_path
        ),
        "backup": lambda child: backup.run_backup_run(
            child, host=io, site_path=site_path, rendered_dir=rendered_dir
        ),
    }


def _runner_stage(name: str, command_path: str, code: int, fix: str) -> StageResult:
    if code == 0:
        return StageResult(name, True, f"{command_path} completed", "")
    return StageResult(name, False, f"{command_path} refused (exit {code})", fix)


def _backup_label_pattern(tag: str) -> re.Pattern[str]:
    return re.compile(rf"^pre-{re.escape(tag)}(?:{_PRE_RELEASE_SET_SUFFIX.pattern})?$")


def _backup_stage(
    io: Host,
    *,
    checkout: Path,
    tag: str,
    target_version: Version,
    target_commit: str,
    from_commit: str,
    runner: Runner,
    now: datetime,
) -> tuple[StageResult, str | None]:
    """The pre-upgrade set: reused only when the checkout already stands at the tag.

    A checkout at the tag's commit is the one durable proof that an earlier attempt
    crossed the checkout, so the running stack is what that attempt's set describes
    and a new set of it would be no rollback material.  Any other attempt takes a
    fresh full set, suffixed when the plain label is taken by an attempt that never
    crossed.
    """

    fix = _BEFORE_NEW_TREE_FIX.format(tag=tag)
    try:
        refs = backupset.list_sets(io)
    except OSError as exc:
        return StageResult("backup", False, f"cannot list backup sets: {exc}", fix), None
    pattern = _backup_label_pattern(tag)
    if from_commit == target_commit:
        reusable = next(
            (
                ref
                for ref in refs
                if ref.manifest is not None
                and pattern.fullmatch(ref.label) is not None
                and ref.manifest.release != str(target_version)
            ),
            None,
        )
        if reusable is None or reusable.manifest is None:
            return (
                StageResult(
                    "backup",
                    False,
                    f"checkout is already at {tag}, but no pre-upgrade set for an "
                    "earlier release remains",
                    "Restore an earlier set by hand with sudo python3 -m gideon restore "
                    "--from staging --set <label>, then retry upgrade.",
                ),
                None,
            )
        return (
            StageResult(
                "backup",
                True,
                f"reused {reusable.label} taken at {reusable.manifest.finished.isoformat()} "
                f"for commit {reusable.manifest.commit}",
                "",
            ),
            reusable.label,
        )

    labels = {ref.label.removesuffix(backupset.PARTIAL_SUFFIX) for ref in refs}
    plain = f"pre-{tag}"
    label = plain
    candidate_time = now
    while label in labels:
        label = f"{plain}-{backupset.nightly_label(candidate_time)}"
        candidate_time += timedelta(seconds=1)
    child = argparse.Namespace(command_path="backup run", full=True, label=label)
    result = _runner_stage("backup", "backup run", runner(child), fix)
    if not result.ok:
        return result, None
    # The set is the rollback's record: its manifest must name the commit the
    # checkout is leaving, or rollback could never find where to return.
    try:
        taken = next((ref for ref in backupset.list_sets(io) if ref.label == label), None)
    except OSError as exc:
        return StageResult("backup", False, f"cannot re-read backup sets: {exc}", fix), None
    if taken is None or taken.manifest is None:
        return StageResult("backup", False, f"set {label} is not complete after backup run", fix), None
    if taken.manifest.commit != from_commit:
        return (
            StageResult(
                "backup",
                False,
                f"set {label} records commit {taken.manifest.commit}, not the checkout's "
                f"{from_commit}, so a rollback could not find it",
                f"Make git -C {checkout} rev-parse HEAD answer as root, then re-run "
                f"{_UPGRADE} {tag}.",
            ),
            None,
        )
    detail = f"fresh pre-upgrade set {label}"
    if label != plain:
        detail += f" ({plain} exists from an attempt that never crossed the checkout and may predate later writes)"
    return StageResult("backup", True, detail, ""), label


def _write_audit(
    audit_api: Any,
    io: Host,
    rendered_dir: PathLike,
    row: audit_module.AuditRow,
    *,
    stage: str,
    detail: str,
    fix: str,
) -> StageResult:
    problem = audit_api.write_rows(io, rendered_dir, (row,))
    if problem is not None:
        return StageResult(stage, False, f"{detail}: {problem}", fix)
    return StageResult(stage, True, detail, "")


def _child(
    io: Host,
    executable: str,
    checkout: Path,
    arguments: Sequence[str],
    *,
    passthrough: bool,
) -> subprocess.CompletedProcess[str]:
    """The tree at *checkout* run through its own CLI, never through this process."""

    return io.run(
        [executable, "-m", "gideon", *arguments], cwd=checkout, passthrough=passthrough
    )


def _child_stage(
    io: Host,
    *,
    executable: str,
    checkout: Path,
    name: str,
    arguments: Sequence[str],
    fix: str,
) -> StageResult:
    """A stage the new tree runs itself, its rows streamed to the operator."""

    command_path = " ".join(arguments)
    try:
        result = _child(io, executable, checkout, arguments, passthrough=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult(name, False, f"{command_path} failed: {exc}", fix)
    if result.returncode != 0:
        return StageResult(name, False, f"{command_path} failed (exit {result.returncode}); its rows above carry the fix", fix)
    return StageResult(name, True, f"{command_path} completed", "")


def _checkout_stage(io: Host, plan: UpgradePlan, owner: _CheckoutOwner) -> StageResult:
    if plan.from_commit == plan.target_commit:
        return StageResult("checkout", True, f"checkout already at {plan.tag}", "")
    fix = _BEFORE_NEW_TREE_FIX.format(tag=plan.tag)
    try:
        result = _run_git(io, plan.checkout, owner, ("checkout", "--detach", plan.tag))
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("checkout", False, f"git checkout failed: {exc}", fix)
    if result.returncode != 0:
        return StageResult("checkout", False, f"git checkout failed: {command_detail(result)}", fix)
    return StageResult("checkout", True, f"checked out {plan.tag} as {plan.owner}", "")


def _version_probe(
    io: Host, *, executable: str, checkout: Path, target: Version
) -> str | None:
    """The problem with the new tree's ``--version`` answer, or None when it is the tag's."""

    try:
        result = _child(io, executable, checkout, ("--version",), passthrough=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"version probe failed: {exc}"
    if result.returncode != 0:
        return f"version probe failed: {command_detail(result)}"
    expected = f"gideon {target}"
    if result.stdout.strip() != expected:
        return f"new tree reported {result.stdout.strip() or 'no version'}, expected {expected}"
    return None


def _applied_release(io: Host, rendered_dir: PathLike) -> str | None:
    try:
        document = yaml.safe_load(io.read_text(Path(rendered_dir) / "applied.yaml"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(document, Mapping):
        return None
    release = document.get("release")
    return release if isinstance(release, str) else None


@dataclass(frozen=True, slots=True)
class _Stack:
    """A fresh Compose read: the services the rendered project declares, and their rows."""

    expected: frozenset[str]
    rows: tuple[Mapping[str, object], ...]

    def problem(self) -> str | None:
        """The first declared service without a container, not running, or not healthy."""

        by_name = {
            str(row["Service"]): row for row in self.rows if isinstance(row.get("Service"), str)
        }
        for service in sorted(self.expected):
            row = by_name.get(service)
            if row is None:
                return f"service {service} has no container"
            if row.get("State") != "running":
                return f"service {service} is not running"
            if row.get("Health", "") not in ("", "healthy"):
                return f"service {service} is not healthy"
        return None

    def whole(self) -> bool:
        """Every declared service running: the state a safety set can be taken from."""

        running = {str(row.get("Service")) for row in self.rows if row.get("State") == "running"}
        return self.expected <= running


def _read_stack(io: Host, rendered_dir: PathLike) -> _Stack | str:
    """The declared services and their containers on a fresh read, or the problem.

    ``compose ps`` lists containers only, so a service that was never created is
    absent from it; the declared set from ``compose config`` is what "every
    service" is judged against.
    """

    try:
        declared = io.run(stack.compose_argv(rendered_dir, "config", "--services"))
        listed = io.run(stack.compose_argv(rendered_dir, "ps", "--all", "--format", "json"))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Compose service check failed: {exc}"
    for result in (declared, listed):
        if result.returncode != 0:
            return f"Compose service check failed: {command_detail(result)}"
    expected = frozenset(line.strip() for line in declared.stdout.splitlines() if line.strip())
    if not expected:
        return "the rendered project declares no services"
    rows = stack.parse_ps(listed.stdout)
    if rows is None:
        return "Compose service check returned invalid JSON"
    return _Stack(expected, rows)


def _verify_stage(
    io: Host, *, executable: str, checkout: Path, rendered_dir: PathLike, target: Version
) -> StageResult:
    """The new tree answers with the tag's version, the applied record names it, the stack is whole."""

    problem = _version_probe(io, executable=executable, checkout=checkout, target=target)
    if problem is None:
        applied_release = _applied_release(io, rendered_dir)
        if applied_release != str(target):
            problem = f"applied record names {applied_release or 'no release'}, expected {target}"
    if problem is None:
        current = _read_stack(io, rendered_dir)
        problem = current if isinstance(current, str) else current.problem()
    if problem is not None:
        return StageResult("verify", False, problem, _AFTER_APPLY_FIX)
    return StageResult(
        "verify",
        True,
        f"new tree reports gideon {target}; the applied record names it; every service is running and healthy",
        "",
    )


def _release_tag(text: str) -> str | None:
    """*text* when it is a tag of the release grammar, else None."""

    try:
        Version.from_tag(text)
    except ValueError:
        return None
    return text


def _release_tag_for_set(label: str) -> str | None:
    """The tag a pre-upgrade set's label names: ``pre-<tag>`` or ``pre-<tag>-<timestamp>``.

    An operator's other ``pre-*`` labels and the ``pre-rollback-*`` safety sets
    name no tag and are never rollback material.
    """

    if not label.startswith("pre-"):
        return None
    return _release_tag(_PRE_RELEASE_SET_SUFFIX.sub("", label.removeprefix("pre-")))


def _rollback_select_stage(
    io: Host,
    *,
    checkout: Path,
    rendered_dir: PathLike,
    owner: _CheckoutOwner,
    current_version: Version,
    tag: str | None,
    marker: _Marker | None,
) -> tuple[StageResult, _RollbackPlan | None]:
    try:
        refs = backupset.list_sets(io)
    except OSError as exc:
        return StageResult("select", False, f"cannot list backup sets: {exc}", _ROLLBACK_FIX), None

    candidates: list[tuple[backupset.SetRef, str, backupset.Manifest]] = []
    for ref in refs:
        if not ref.complete or ref.manifest is None:
            continue
        release_tag = _release_tag_for_set(ref.label)
        if release_tag is None or (tag is not None and release_tag != tag):
            continue
        candidates.append((ref, release_tag, ref.manifest))
    if not candidates:
        target_text = f" for {tag}" if tag is not None else ""
        return (
            StageResult(
                "select",
                False,
                f"no complete pre-upgrade set is available{target_text}",
                _ROLLBACK_BY_HAND_FIX,
            ),
            None,
        )

    selected, _, manifest = max(candidates, key=lambda item: item[2].finished)
    try:
        commit = _run_git(
            io,
            checkout,
            owner,
            ("cat-file", "-e", f"{manifest.commit}^{{commit}}"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("select", False, f"cannot verify set commit: {exc}", _ROLLBACK_FETCH_FIX), None
    if commit.returncode != 0:
        return (
            StageResult(
                "select",
                False,
                f"set {selected.label} names commit {manifest.commit}, which is not in the checkout",
                _ROLLBACK_FETCH_FIX,
            ),
            None,
        )
    try:
        target_version = Version.parse(manifest.release)
    except ValueError as exc:
        return (
            StageResult("select", False, f"set {selected.label} has an invalid release: {exc}", _ROLLBACK_BY_HAND_FIX),
            None,
        )
    resumed = ""
    if current_version == target_version:
        # The tree already runs the set's release: a finished rollback when no
        # rollback is in progress and the applied record names it too, else one
        # that stopped after its checkout and resumes here from the previous tree.
        applied_release = _applied_release(io, rendered_dir)
        if marker is None and applied_release == manifest.release:
            return (
                StageResult(
                    "select",
                    False,
                    f"the running tree and the applied record are both at {manifest.release}; "
                    "there is nothing to roll back to",
                    f"Run sudo python3 -m gideon restore --from staging --set {selected.label} "
                    "for the data alone.",
                ),
                None,
            )
        resumed = (
            f"; resuming after a checkout to {manifest.release} (the applied record names "
            f"{applied_release or 'no release'})"
        )
    plan = _RollbackPlan(
        checkout=checkout,
        owner=owner,
        current_version=current_version,
        target_version=target_version,
        target_release=manifest.release,
        target_commit=manifest.commit,
        set_label=selected.label,
        archive_through=manifest.archive_through,
    )
    return (
        StageResult(
            "select",
            True,
            f"selected {selected.label}; release={manifest.release}; commit={manifest.commit}; "
            f"archive_through={manifest.archive_through.isoformat()}{resumed}",
            "",
        ),
        plan,
    )


def _restore_verdict(io: Host, *, rendered_dir: PathLike, release: str) -> tuple[bool, str]:
    """Whether a restore is needed, judged from the rendered manifest, and why.

    render_to_disk writes the manifest as apply's first act before any stage
    touches the stores; a manifest still naming the set's release proves the new
    tree's apply never completed a render.  applied.yaml is never consulted: it is
    written only after verify, so an old value says nothing about a failed apply.
    """

    manifest_path = Path(rendered_dir) / "manifest.yaml"
    try:
        text = io.read_text(manifest_path)
    except FileNotFoundError:
        return True, "rendered manifest is missing"
    except (OSError, UnicodeError) as exc:
        return True, f"rendered manifest is unreadable ({exc})"
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return True, f"rendered manifest is unparsable ({exc})"
    if not isinstance(document, Mapping):
        return True, "rendered manifest is not a mapping"
    if document.get("release") == release:
        return False, f"rendered manifest names {release}, so the new release never completed a render"
    return True, f"rendered manifest names {document.get('release', 'no release')}, not {release}"


def _rollback_plan_stage(
    io: Host, *, rendered_dir: PathLike, plan: _RollbackPlan, marker: _Marker | None, now: datetime
) -> tuple[StageResult, _RollbackPlan | None, _Marker | None]:
    """Decide whether this run restores, resuming a rollback in progress when one is recorded."""

    if marker is not None:
        if marker.set_label != plan.set_label:
            return (
                StageResult(
                    "plan",
                    False,
                    f"a rollback of {marker.set_label} begun {marker.started} is in progress",
                    f"Re-run {_ROLLBACK} {_release_tag_for_set(marker.set_label) or ''} to finish it, "
                    f"or {_MARKER_FIX}",
                ),
                None,
                None,
            )
        needed = marker.restore_needed and not marker.restored
        state = (
            "the restore is still pending"
            if needed
            else ("the restore is done" if marker.restore_needed else "checkout-only")
        )
        detail = f"resuming the rollback of {marker.set_label} begun {marker.started}; {state}"
        return StageResult("plan", True, detail, ""), replace(plan, restore_needed=needed), marker
    needed, reason = _restore_verdict(io, rendered_dir=rendered_dir, release=plan.target_release)
    fresh = _Marker(plan.set_label, needed, False, now.isoformat())
    problem = _write_marker(io, fresh)
    if problem is not None:
        return StageResult("plan", False, problem, _MARKER_FIX), None, None
    verdict = "restore needed" if needed else "checkout-only"
    return StageResult("plan", True, f"{verdict}; {reason}", ""), replace(plan, restore_needed=needed), fresh


def _rollback_safety_stage(
    io: Host,
    *,
    rendered_dir: PathLike,
    plan: _RollbackPlan,
    runner: Runner,
    now: datetime,
) -> StageResult:
    """A whole running stack gets a safety set from the current tree before anything stops."""

    if not plan.restore_needed:
        return StageResult("safety", True, "skipped (the new release never applied)", "")
    docker_fix = f"Check that Docker is running (systemctl status docker), then re-run {_ROLLBACK}."
    current = _read_stack(io, rendered_dir)
    if isinstance(current, str):
        return StageResult("safety", False, current, docker_fix)
    if not current.whole():
        return StageResult(
            "safety",
            True,
            "partial or stopped stack; anything written since the upgrade began is not preserved",
            "",
        )
    label = f"pre-rollback-{backupset.nightly_label(now)}"
    code = runner(argparse.Namespace(command_path="backup run", full=True, label=label))
    if code != 0:
        return StageResult("safety", False, f"backup run refused (exit {code})", _ROLLBACK_SAFETY_FIX)
    return StageResult("safety", True, f"whole stack running; safety set {label} taken", "")


def _rollback_stop_stage(io: Host, rendered_dir: PathLike) -> StageResult:
    try:
        result = io.run(stack.compose_argv(rendered_dir, "down"))
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("stop", False, f"Compose down failed: {exc}", _ROLLBACK_FIX)
    if result.returncode != 0:
        return StageResult("stop", False, f"Compose down failed: {command_detail(result)}", _ROLLBACK_FIX)
    return StageResult("stop", True, "stack stopped", "")


def _rollback_identity_stage(io: Host, plan: _RollbackPlan) -> StageResult:
    """Remove the box identity when the selected tree cannot exclude it."""

    try:
        refs = backupset.list_sets(io)
    except OSError as exc:
        return StageResult("identity", False, f"cannot list backup sets: {exc}", _ROLLBACK_FIX)
    selected = next((ref for ref in refs if ref.label == plan.set_label), None)
    if selected is None or selected.manifest is None:
        return StageResult(
            "identity",
            False,
            f"selected backup set {plan.set_label} was not found",
            _ROLLBACK_FIX,
        )

    try:
        io.stat(AGE_IDENTITY_PATH)
    except FileNotFoundError:
        present = False
    except OSError as exc:
        return StageResult(
            "identity",
            False,
            f"cannot inspect {AGE_IDENTITY_PATH}: {exc}",
            _ROLLBACK_FIX,
        )
    else:
        present = True
    recipients = selected.manifest.recipients
    if len(recipients) == 1:
        if not present:
            return StageResult(
                "identity",
                True,
                "box identity already absent: the set's tree seals to one recipient",
                "",
            )
        try:
            io.unlink(AGE_IDENTITY_PATH, missing_ok=True)
        except OSError as exc:
            return StageResult(
                "identity",
                False,
                f"box identity could not be removed: {exc}",
                _ROLLBACK_FIX,
            )
        return StageResult(
            "identity",
            True,
            "box identity removed: the set's tree seals to one recipient",
            "",
        )
    detail = (
        "box identity kept" if present else "box identity already absent"
    )
    return StageResult(
        "identity",
        True,
        f"{detail}: the set's tree seals to {len(recipients)} recipients",
        "",
    )


def _rollback_checkout_stage(io: Host, plan: _RollbackPlan) -> StageResult:
    try:
        head = _run_git(io, plan.checkout, plan.owner, ("rev-parse", "HEAD"))
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("checkout", False, f"current commit could not be read: {exc}", _ROLLBACK_FIX)
    if head.returncode != 0:
        return StageResult("checkout", False, f"current commit could not be read: {command_detail(head)}", _ROLLBACK_FIX)
    current_commit = next((line.strip() for line in head.stdout.splitlines() if line.strip()), None)
    if current_commit == plan.target_commit:
        return StageResult("checkout", True, f"checkout already at {plan.target_commit}", "")
    try:
        tags = _run_git(
            io,
            plan.checkout,
            plan.owner,
            ("tag", "--points-at", plan.target_commit),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("checkout", False, f"git tag lookup failed: {exc}", _ROLLBACK_FETCH_FIX)
    if tags.returncode != 0:
        return StageResult("checkout", False, f"git tag lookup failed: {command_detail(tags)}", _ROLLBACK_FETCH_FIX)
    ref = next(
        (candidate for candidate in tags.stdout.splitlines() if _release_tag(candidate.strip())),
        plan.target_commit,
    )
    ref = ref.strip()
    try:
        result = _run_git(io, plan.checkout, plan.owner, ("checkout", "--detach", ref))
    except (OSError, subprocess.SubprocessError) as exc:
        return StageResult("checkout", False, f"git checkout failed: {exc}", _ROLLBACK_FIX)
    if result.returncode != 0:
        return StageResult("checkout", False, f"git checkout failed: {command_detail(result)}", _ROLLBACK_FIX)
    return StageResult("checkout", True, f"checked out {ref} for {plan.target_commit} as {plan.owner.name}", "")


def _run_rollback(
    args: argparse.Namespace,
    *,
    io: LockingHost,
    site_path: PathLike,
    rendered_dir: PathLike,
    checkout: Path,
    runners: Mapping[str, Runner] | None,
    executable: str,
    now: datetime | None,
    audit_api: Any,
) -> int:
    """Restore the pre-upgrade set and re-apply the release it describes."""

    tag = getattr(args, "tag", None)
    if tag is not None and (not isinstance(tag, str) or _release_tag(tag) is None):
        return _refuse(f"target tag is invalid: {tag}.", _TAG_GRAMMAR_FIX)
    effective_now = aware_now(now)
    if effective_now is None:
        return _refuse("upgrade's clock value must be timezone-aware.", "Use an aware UTC time, then retry.")
    phase_runners = (
        runners
        if runners is not None
        else _default_runners(io, site_path=site_path, rendered_dir=rendered_dir)
    )
    timer = _StageTimer()
    with timer.timed("preconditions"):
        preconditions, owner = _preconditions(io, checkout=checkout, retry=_ROLLBACK)
    print_stage(preconditions)
    if not preconditions.ok or owner is None:
        return 1

    marker = _read_marker(io)
    if isinstance(marker, str):
        print_stage(StageResult("select", False, marker, _MARKER_FIX))
        return 1
    with timer.timed("select"):
        select_result, plan = _rollback_select_stage(
            io,
            checkout=checkout,
            rendered_dir=rendered_dir,
            owner=owner,
            current_version=Version.parse(gideon.__version__),
            tag=tag,
            marker=marker,
        )
    print_stage(select_result)
    if not select_result.ok or plan is None:
        return 1

    with timer.timed("plan"):
        plan_result, planned, marker = _rollback_plan_stage(
            io, rendered_dir=rendered_dir, plan=plan, marker=marker, now=effective_now
        )
    print_stage(plan_result)
    if not plan_result.ok or planned is None or marker is None:
        return 1
    plan = planned

    with timer.timed("safety"):
        safety_result = _rollback_safety_stage(
            io,
            rendered_dir=rendered_dir,
            plan=plan,
            runner=phase_runners["backup"],
            now=effective_now,
        )
    print_stage(safety_result)
    if not safety_result.ok:
        return 1

    run_id = str(uuid.uuid4())

    def row(phase: str, **detail: object) -> audit_module.AuditRow:
        return audit_module.AuditRow(
            run_id,
            "rollback",
            None,
            None,
            None,
            (),
            {"phase": phase, "set_label": plan.set_label, **detail},
        )

    with timer.timed("audit-intent"):
        intent_attempt = _write_audit(
            audit_api,
            io,
            rendered_dir,
            row(
                "intent",
                from_version=str(plan.current_version),
                to_release=plan.target_release,
                commit=plan.target_commit,
                restore_needed=plan.restore_needed,
            ),
            stage="audit-intent",
            detail="rollback intent recorded",
            fix=_ROLLBACK_FIX,
        )
    intent_recorded = intent_attempt.ok
    if intent_attempt.ok:
        intent_result = intent_attempt
    else:
        intent_result = StageResult(
            "audit-intent",
            True,
            f"rollback intent deferred: {intent_attempt.detail}",
            "",
        )
    print_stage(intent_result)

    def failed(phase: str) -> int:
        with timer.timed("audit-applied"):
            applied_result = _write_audit(
                audit_api,
                io,
                rendered_dir,
                row(
                    "failed",
                    failed_phase=phase,
                    durations=dict(timer.durations),
                    intent_recorded=intent_recorded,
                ),
                stage="audit-applied",
                detail=f"rollback failure recorded at {phase}",
                fix=_ROLLBACK_FIX,
            )
        print_stage(applied_result)
        return 1

    with timer.timed("stop"):
        stop_result = (
            _rollback_stop_stage(io, rendered_dir)
            if plan.restore_needed
            else StageResult("stop", True, "skipped (the new release never applied)", "")
        )
    print_stage(stop_result)
    if not stop_result.ok:
        return failed("stop")

    with timer.timed("identity"):
        identity_result = _rollback_identity_stage(io, plan)
    print_stage(identity_result)
    if not identity_result.ok:
        return failed("identity")

    with timer.timed("checkout"):
        checkout_result = _rollback_checkout_stage(io, plan)
    print_stage(checkout_result)
    if not checkout_result.ok:
        return failed("checkout")

    if plan.restore_needed:
        with timer.timed("restore"):
            restore_result = _child_stage(
                io,
                executable=executable,
                checkout=plan.checkout,
                name="restore",
                arguments=("restore", "--from", "staging", "--set", plan.set_label),
                fix=_ROLLBACK_FIX,
            )
        if restore_result.ok:
            problem = _write_marker(io, replace(marker, restored=True))
            if problem is not None:
                restore_result = StageResult(
                    "restore", False, f"restore completed, but the record of it failed: {problem}", _MARKER_FIX
                )
    elif marker.restored:
        restore_result = StageResult("restore", True, "skipped (already restored by the earlier run)", "")
    else:
        restore_result = StageResult("restore", True, "skipped (the new release never applied)", "")
    print_stage(restore_result)
    if not restore_result.ok:
        return failed("restore")

    with timer.timed("apply"):
        apply_result = _child_stage(
            io,
            executable=executable,
            checkout=plan.checkout,
            name="apply",
            arguments=("apply",),
            fix=_ROLLBACK_FIX,
        )
    print_stage(apply_result)
    if not apply_result.ok:
        return failed("apply")

    with timer.timed("verify"):
        verify_result = _verify_stage(
            io,
            executable=executable,
            checkout=plan.checkout,
            rendered_dir=rendered_dir,
            target=plan.target_version,
        )
    print_stage(verify_result)
    if not verify_result.ok:
        return failed("verify")

    with timer.timed("engine-verify"):
        engine_verify_result = _child_stage(
            io,
            executable=executable,
            checkout=plan.checkout,
            name="engine-verify",
            arguments=("engine", "verify"),
            fix=(
                "Do not go live on this engine. Run "
                f"{stack.logs_fix(rendered_dir, ENGINE_SERVICE_NAME)}, correct the engine or driver, "
                "then run sudo python3 -m gideon engine verify by hand."
            ),
        )
    print_stage(engine_verify_result)
    if not engine_verify_result.ok:
        return failed("engine-verify")

    with timer.timed("audit-applied"):
        applied_result = _write_audit(
            audit_api,
            io,
            rendered_dir,
            row(
                "applied",
                durations=dict(timer.durations),
                intent_recorded=intent_recorded,
            ),
            stage="audit-applied",
            detail="rollback applied",
            fix=_ROLLBACK_FIX,
        )
        # The record goes only once the row is written: a re-run after a failed
        # row must still find it, or it would refuse with nothing left to do.
        if applied_result.ok:
            try:
                io.unlink(_MARKER_PATH, missing_ok=True)
            except OSError as exc:
                applied_result = StageResult(
                    "audit-applied",
                    False,
                    f"rollback applied, but the rollback record could not be removed: {exc}",
                    _MARKER_FIX,
                )
    print_stage(applied_result)
    return int(not applied_result.ok)


def run_upgrade(
    args: argparse.Namespace,
    *,
    host: LockingHost | None = None,
    site_path: PathLike = _SITE_PATH,
    rendered_dir: PathLike = _RENDERED_DIR,
    checkout: PathLike | None = None,
    runners: Mapping[str, Runner] | None = None,
    python: str | None = None,
    now: datetime | None = None,
    audit: Any | None = None,
) -> int:
    """Run the forward upgrade or rollback path; exit 0 iff every stage is ok."""

    io = host or RealHost()
    audit_api = audit if audit is not None else audit_module
    rollback = bool(getattr(args, "rollback", False))
    tag = getattr(args, "tag", None)
    retry = _ROLLBACK if rollback else f"{_UPGRADE} {tag or '<tag>'}"
    if io.geteuid() != 0:
        return _refuse("root is required.", f"Run {retry} as root.")
    loaded = site.load_site(Path(site_path), host=io)
    if loaded.errors or loaded.config is None:
        return _refuse(
            site_problem(loaded) or "the site file is invalid.",
            f"Correct /etc/gideon/site.yaml, then re-run {retry}.",
        )
    root = Path(__file__).parents[2] if checkout is None else Path(checkout)
    executable = sys.executable if python is None else python
    if rollback:
        return _run_rollback(
            args,
            io=io,
            site_path=site_path,
            rendered_dir=rendered_dir,
            checkout=root,
            runners=runners,
            executable=executable,
            now=now,
            audit_api=audit_api,
        )
    if not isinstance(tag, str) or not tag:
        return _refuse("a target tag is required.", _TAG_FIX)
    try:
        Version.from_tag(tag)
    except ValueError:
        return _refuse(f"target tag is invalid: {tag}.", _TAG_GRAMMAR_FIX)
    effective_now = aware_now(now)
    if effective_now is None:
        return _refuse(
            "upgrade's clock value must be timezone-aware.", "Use an aware UTC time, then retry upgrade."
        )

    phase_runners = (
        runners
        if runners is not None
        else _default_runners(io, site_path=site_path, rendered_dir=rendered_dir)
    )
    before_fix = _BEFORE_NEW_TREE_FIX.format(tag=tag)
    after_checkout_fix = _AFTER_CHECKOUT_FIX.format(tag=tag)
    audit_fix = f"Check the rendered Postgres service, then re-run {retry}."
    timer = _StageTimer()

    with timer.timed("preconditions"):
        preconditions, owner = _preconditions(io, checkout=root, retry=retry)
    print_stage(preconditions)
    if not preconditions.ok or owner is None:
        return 1

    with timer.timed("fetch"):
        fetched, target_commit, from_commit = _fetch_stage(
            io, checkout=root, owner=owner, tag=tag
        )
    print_stage(fetched)
    if not fetched.ok or target_commit is None or from_commit is None:
        return 1

    with timer.timed("version"):
        version_result, current_version, target_version = _version_stage(
            io,
            checkout=root,
            owner=owner,
            tag=tag,
            target_commit=target_commit,
            acknowledge_breaking=bool(getattr(args, "acknowledge_breaking", False)),
        )
    print_stage(version_result)
    if not version_result.ok or current_version is None or target_version is None:
        return 1

    with timer.timed("preflight"):
        current_preflight = _runner_stage(
            "preflight",
            "preflight",
            phase_runners["preflight"](argparse.Namespace(command_path="preflight")),
            before_fix,
        )
    print_stage(current_preflight)
    if not current_preflight.ok:
        return 1

    with timer.timed("backup"):
        backup_result, set_label = _backup_stage(
            io,
            checkout=root,
            tag=tag,
            target_version=target_version,
            target_commit=target_commit,
            from_commit=from_commit,
            runner=phase_runners["backup"],
            now=effective_now,
        )
    print_stage(backup_result)
    if not backup_result.ok or set_label is None:
        return 1

    plan = UpgradePlan(
        checkout=root,
        owner=owner.name,
        owner_uid=owner.uid,
        current_version=current_version,
        from_commit=from_commit,
        tag=tag,
        target_version=target_version,
        target_commit=target_commit,
        set_label=set_label,
    )
    run_id = str(uuid.uuid4())

    def row(phase: str, **detail: object) -> audit_module.AuditRow:
        return audit_module.AuditRow(
            run_id, "upgrade", None, None, None, (), {"phase": phase, "set_label": plan.set_label, **detail}
        )

    with timer.timed("audit-intent"):
        intent = _write_audit(
            audit_api,
            io,
            rendered_dir,
            row(
                "intent",
                from_version=str(plan.current_version),
                to_tag=plan.tag,
                from_commit=plan.from_commit,
                to_commit=plan.target_commit,
            ),
            stage="audit-intent",
            detail="upgrade intent recorded",
            fix=audit_fix,
        )
    print_stage(intent)
    if not intent.ok:
        return 1

    def failed(phase: str) -> int:
        """After the intent row, a failure is recorded by the old process, then exit 1."""

        with timer.timed("audit-applied"):
            result = _write_audit(
                audit_api,
                io,
                rendered_dir,
                row("failed", failed_phase=phase, durations=dict(timer.durations)),
                stage="audit-applied",
                detail=f"upgrade failure recorded at {phase}",
                fix=audit_fix,
            )
        print_stage(result)
        return 1

    with timer.timed("checkout"):
        checkout_result = _checkout_stage(io, plan, owner)
    print_stage(checkout_result)
    if not checkout_result.ok:
        return failed("checkout")

    # From here on the tree under this process is the new release's; every step is
    # the new tree's own CLI as a child, and nothing below imports anything.
    for name, key, arguments, fix in (
        ("provision", "provision", ("host", "provision"), after_checkout_fix),
        ("preflight", "new-tree-preflight", ("preflight",), after_checkout_fix),
        ("apply", "apply", ("apply",), _AFTER_APPLY_FIX),
    ):
        with timer.timed(key):
            result = _child_stage(
                io,
                executable=executable,
                checkout=plan.checkout,
                name=name,
                arguments=arguments,
                fix=fix,
            )
        print_stage(result)
        if not result.ok:
            return failed(key)

    with timer.timed("verify"):
        verify_result = _verify_stage(
            io,
            executable=executable,
            checkout=plan.checkout,
            rendered_dir=rendered_dir,
            target=plan.target_version,
        )
    print_stage(verify_result)
    if not verify_result.ok:
        return failed("verify")

    with timer.timed("engine-verify"):
        engine_verify_result = _child_stage(
            io,
            executable=executable,
            checkout=plan.checkout,
            name="engine-verify",
            arguments=("engine", "verify"),
            fix=_AFTER_APPLY_FIX,
        )
    print_stage(engine_verify_result)
    if not engine_verify_result.ok:
        return failed("engine-verify")

    with timer.timed("audit-applied"):
        applied = _write_audit(
            audit_api,
            io,
            rendered_dir,
            row("applied", durations=dict(timer.durations)),
            stage="audit-applied",
            detail="upgrade applied",
            fix=audit_fix,
        )
    print_stage(applied)
    return int(not applied.ok)
