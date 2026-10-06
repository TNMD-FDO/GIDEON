"""A backup set's roots, bound to where their copy sits.

Each kind decides what it does at each moment of a backup or a restore; a
command iterates the bound roots and never names one or reads its kind.
"""

import os
import re
import stat
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

from gideon.host import backupset, cas, pgbackrest, report, sshtarget
from gideon.host.report import Problem, command_detail
from gideon.host.stages import run_stage
from gideon.host.sysio import CompletedText, Host, PathLike

# Paths per chown, chmod, or sha256sum argv: far below the kernel's argument limit.
_CHUNK_SIZE: Final = 200
LINK_FIX: Final = "Confirm /data/backup-staging is one filesystem, then re-run backup run"
LINK_PROBLEM: Final = "no hard links to the previous set; a full copy would hide a retention overrun"
_STORE_SHAPE_FIX: Final = "Move the named path out of the live content-addressed store, then re-run backup run."
_OBJECT_NAME: Final = re.compile(r"[0-9a-f]{64}")
_OBJECT_REGEX: Final =r"\./\([0-9a-f]\{2\}\)/\([0-9a-f]\{2\}\)/\1\2[0-9a-f]\{60\}"
_REGULAR_TRANSFER = re.compile(r"^Number of regular files transferred:\s*([0-9][0-9,]*)\s*$", re.MULTILINE)
NOTHING_LEFT_OUT_DETAIL: Final = "0 left out"
# Lines per sha256sum -c batch on stdin, and names in a closing line: display
# and memory bounds, not thresholds.
_HASH_BATCH: Final = 1000
_LEFT_OUT_NAMED: Final = 10


def _physical_fix() -> str:
    return (
        "The fetched or restored tree does not match its manifest; re-run "
        f"{report.command('backup push --verify-all')} on the source box, "
        "then retry restore."
    )


def _object_relative(name: str) -> str:
    """An object's path under its store root: its two shard directories, then its name."""

    return f"{name[:2]}/{name[2:4]}/{name}"


def _store_copy_argv(source: str, destination: str, exclusions: Sequence[str], *extra: str) -> list[str]:
    return [
        "rsync", "-rtp", "--stats",
        *(f"--exclude={pattern}" for pattern in exclusions),
        *extra,
        source.rstrip("/") + "/", destination.rstrip("/") + "/",
    ]


def _checked(
    io: Host, argv: Sequence[str], what: str, *, cwd: str | None = None, input: str | None = None
) -> CompletedText | Problem:
    """Run *argv*; a failure to start or a non-zero exit is the problem, with no fix of its own."""

    try:
        if input is None:
            result = io.run(list(argv), cwd=cwd)
        else:
            result = io.run(list(argv), cwd=cwd, input=input)
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"{what} failed: {exc}", "")
    if result.returncode != 0:
        return Problem(f"{what} failed: {command_detail(result)}", "")
    return result


def _transferred(io: Host, argv: Sequence[str], what: str, *, input: str | None = None) -> int | Problem:
    """Run a store transfer and return the regular files its own report says it wrote."""

    result = _checked(io, argv, what, input=input)
    if isinstance(result, Problem):
        return result
    match = _REGULAR_TRANSFER.search(result.stdout)
    if match is None:
        return Problem(f"{what} reported no transferred file count", "")
    return int(match.group(1).replace(",", ""))


# GNU find's default regular-expression type ignores the \{n\} intervals; the
# basic type honours them, and its back-references hold an object's name to
# its own two shard directories.
def _store_stray_argv() -> list[str]:
    return [
        "find", ".", "-regextype", "posix-basic", "-mindepth", "1",
        "!", "(", "-type", "d", "-regex", r"\./[0-9a-f]\{2\}", ")",
        "!", "(", "-type", "d", "-regex", r"\./[0-9a-f]\{2\}/[0-9a-f]\{2\}", ")",
        "!", "(", "-type", "f", "-regex", _OBJECT_REGEX, ")",
        "-print", "-quit",
    ]


def _store_listing_argv(listing: str) -> list[str]:
    """The objects under the working directory, one name and size per line, written by find itself."""

    return [
        "find", ".", "-regextype", "posix-basic", "-type", "f", "-regex", _OBJECT_REGEX,
        "-fprintf", listing, r"%f\t%s\n",
    ]


def _store_sort_argv(listing: str) -> list[str]:
    return ["env", "LC_ALL=C", "sort", "-o", listing, listing]


def _listing_pin(io: Host, listing: str) -> backupset.ListingPin | Problem:
    """Pin a sorted listing by its size and hash, with its object count and byte total."""

    hashed = _checked(io, ["sha256sum", listing], f"hashing {listing}")
    if isinstance(hashed, Problem):
        return hashed
    # %.0f rather than %d, so an awk keeping numbers as doubles prints a large total exactly.
    counted = _checked(
        io, ["awk", "-F", "\t", '{n++; b+=$2} END {printf "%.0f %.0f\\n", n, b}', listing], f"counting {listing}"
    )
    if isinstance(counted, Problem):
        return counted
    try:
        size = io.stat(listing).st_size
        digest = backupset.parse_sha256sum(hashed.stdout)[listing]
        objects_text, bytes_text = counted.stdout.split()
        return backupset.ListingPin(
            os.path.basename(listing), size, digest, int(objects_text, 10), int(bytes_text, 10)
        )
    except (OSError, KeyError, ValueError) as exc:
        return Problem(f"pinning {listing} failed: {exc}", "")


@dataclass(frozen=True, slots=True)
class Taken:
    """A root's take: whether a copy was made, and the pairs the link sample may compare."""

    copied: bool
    link_pairs: Mapping[str, tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Completed:
    """Whether a root completed a second copy and how many objects arrived."""

    acted: bool
    objects: int


@dataclass(frozen=True, slots=True)
class Inventoried:
    """A root's inventory, sorted and hashed, and the bytes its copy adds to the set."""

    entries: tuple[backupset.Entry, ...]
    set_bytes: int
    pin: backupset.ListingPin | None = None


@dataclass(frozen=True, slots=True)
class Checked:
    """Mandatory and sampled target-relative paths paired with their hashes."""

    mandatory: tuple[tuple[str, str], ...]
    sampled: tuple[tuple[str, str], ...]


def _relative_hashes(directory: str, hashes: Mapping[str, str]) -> Mapping[str, str]:
    prefix = directory.rstrip("/") + "/"
    return {
        path.removeprefix(prefix) if path.startswith(prefix) else path: digest
        for path, digest in hashes.items()
    }


def _find_entries(io: Host, directory: str) -> tuple[backupset.Entry, ...] | Problem:
    try:
        found = io.run(["find", directory, "-printf", backupset.FIND_FORMAT])
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"find failed for {directory}: {exc}", "")
    if found.returncode != 0:
        return Problem(f"find failed for {directory}: {command_detail(found)}", "")
    try:
        return tuple(sorted(backupset.parse_find_listing(found.stdout), key=lambda entry: entry.path))
    except ValueError as exc:
        return Problem(f"find listing was malformed for {directory}: {exc}", "")


def _hash_entries(
    io: Host,
    directory: str,
    entries: tuple[backupset.Entry, ...],
    *,
    repository: bool,
    previous_entries: Mapping[str, backupset.Entry],
) -> tuple[backupset.Entry, ...] | Problem:
    carried = tuple(
        backupset.carry_forward(previous_entries, entry) if repository else entry
        for entry in entries
    )
    paths = tuple(
        os.path.join(directory, entry.path)
        for entry in carried
        if entry.kind == "f" and entry.sha256 is None
    )
    hashes: dict[str, str] = {
        entry.path: entry.sha256
        for entry in carried
        if entry.kind == "f" and entry.sha256 is not None
    }
    commands: Iterable[list[str]]
    if repository:
        all_paths = paths
        commands = (
            ["sha256sum", "--", *all_paths[start : start + _CHUNK_SIZE]]
            for start in range(0, len(all_paths), _CHUNK_SIZE)
        )
    else:
        commands = iter(
            [["find", directory, "-type", "f", "-exec", "sha256sum", "{}", "+"]]
        )
    for argv in commands:
        try:
            result = io.run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            return Problem(f"hashing failed for {directory}: {exc}", "")
        if result.returncode != 0:
            return Problem(f"hashing failed for {directory}: {command_detail(result)}", "")
        try:
            parsed = backupset.parse_sha256sum(result.stdout)
        except ValueError as exc:
            return Problem(f"hash listing was malformed for {directory}: {exc}", "")
        hashes.update(_relative_hashes(directory, parsed))
    merged = backupset.merge_hashes(carried, hashes)
    if isinstance(merged, Problem):
        return merged
    return tuple(sorted(merged, key=lambda entry: entry.path))


@dataclass(frozen=True, slots=True)
class TakingRoot:
    """A root bound to the in-flight set, the previous set, and its live source."""

    io: Host
    name: str
    source: str
    exclusions: tuple[str, ...]
    partial: str
    previous: backupset.SetRef | None

    def take(self) -> Taken | Problem:
        raise NotImplementedError

    def inventory(self) -> Inventoried | Problem:
        raise NotImplementedError

    def complete(self) -> Completed | Problem:
        raise NotImplementedError

    def _copy(self) -> str:
        return os.path.join(backupset.files_dir(self.partial), self.name)

    def _previous_entries(self) -> Mapping[str, backupset.Entry]:
        entries: Sequence[backupset.Entry] = ()
        if self.previous is not None and self.previous.manifest is not None:
            entries = self.previous.manifest.inventory.get(self.name, ())
        return {entry.path: entry for entry in entries}


@dataclass(frozen=True, slots=True)
class _TakingSnapshot(TakingRoot):
    def take(self) -> Taken | Problem:
        destination = self._copy()
        try:
            self.io.mkdir(destination, mode=0o750, parents=True, exist_ok=True)
            argv = ["rsync", "-a"]
            argv.extend(f"--exclude={pattern}" for pattern in self.exclusions)
            if self.previous is not None:
                link_dest = os.path.join(backupset.files_dir(self.previous.path), self.name)
                argv.append(f"--link-dest={link_dest}/")
            argv.extend(
                [
                    self.source.rstrip("/") + "/",
                    destination.rstrip("/") + "/",
                ]
            )
            result = self.io.run(argv)
            if result.returncode != 0:
                return Problem(f"rsync failed for {self.name}: {command_detail(result)}", "")
        except (OSError, subprocess.SubprocessError) as exc:
            return Problem(f"file snapshot failed: {exc}", "")

        pairs: dict[str, tuple[str, str]] = {}
        if self.previous is not None and self.previous.manifest is not None:
            for entry in self.previous.manifest.inventory.get(self.name, ()):
                if entry.kind == "f":
                    key = f"{self.name}/{entry.path}"
                    pairs[key] = (
                        os.path.join(destination, entry.path),
                        os.path.join(backupset.files_dir(self.previous.path), self.name, entry.path),
                    )
        return Taken(True, pairs)

    def inventory(self) -> Inventoried | Problem:
        directory = self._copy()
        entries = _find_entries(self.io, directory)
        if isinstance(entries, Problem):
            return entries
        hashed = _hash_entries(
            self.io, directory, entries, repository=False, previous_entries=self._previous_entries()
        )
        if isinstance(hashed, Problem):
            return hashed
        return Inventoried(hashed, sum(entry.size for entry in hashed if entry.kind == "f"))

    def complete(self) -> Completed | Problem:
        return Completed(False, 0)


@dataclass(frozen=True, slots=True)
class _TakingRepository(TakingRoot):
    def take(self) -> Taken | Problem:
        return Taken(False, {})

    def inventory(self) -> Inventoried | Problem:
        directory = self.source
        entries = _find_entries(self.io, directory)
        if isinstance(entries, Problem):
            return entries
        hashed = _hash_entries(
            self.io, directory, entries, repository=True, previous_entries=self._previous_entries()
        )
        if isinstance(hashed, Problem):
            return hashed
        return Inventoried(hashed, 0)

    def complete(self) -> Completed | Problem:
        return Completed(False, 0)


@dataclass(frozen=True, slots=True)
class _TakingStore(TakingRoot):
    def _transfer(self, *, first: bool) -> int | Problem:
        destination = self._copy()
        try:
            if not self.io.exists(self.source):
                return Problem(f"content-addressed store root is missing: {self.source}", cas.PROVISION_FIX)
            self.io.mkdir(destination, mode=0o750, parents=True, exist_ok=True)
        except OSError as exc:
            return Problem(f"store copy failed for {self.name}: {exc}", "")
        extra: tuple[str, ...] = ()
        if (
            first
            and self.previous is not None
            and self.previous.manifest is not None
            and self.name in self.previous.manifest.listings
        ):
            previous_copy = os.path.join(backupset.files_dir(self.previous.path), self.name)
            extra = (f"--link-dest={previous_copy}/",)
        argv = _store_copy_argv(self.source, destination, self.exclusions, *extra)
        return _transferred(self.io, argv, f"store copy for {self.name}")

    def take(self) -> Taken | Problem:
        transferred = self._transfer(first=True)
        if isinstance(transferred, Problem):
            return transferred
        if self.previous is not None and self.previous.manifest is not None:
            pin = self.previous.manifest.listings.get(self.name)
            if pin is not None and pin.objects > 0:
                linked = _checked(
                    self.io,
                    ["find", self._copy(), "-type", "f", "-links", "+1", "-print", "-quit"],
                    f"store link check for {self.name}",
                )
                if isinstance(linked, Problem):
                    return linked
                if not linked.stdout.strip():
                    return Problem(LINK_PROBLEM, LINK_FIX)
        return Taken(True, {})

    def complete(self) -> Completed | Problem:
        transferred = self._transfer(first=False)
        if isinstance(transferred, Problem):
            return transferred
        return Completed(True, transferred)

    def inventory(self) -> Inventoried | Problem:
        copy = self._copy()
        listing = backupset.listing_path(self.partial, self.name)
        stray = _checked(self.io, _store_stray_argv(), f"store shape check for {self.name}", cwd=copy)
        if isinstance(stray, Problem):
            return stray
        if stray.stdout.strip():
            # The copy mirrors the live store, so the offender is named where it can be moved.
            offender = os.path.join(self.source, stray.stdout.strip().removeprefix("./"))
            return Problem(f"{offender} is not an object in its own shard directories", _STORE_SHAPE_FIX)
        for argv, what, cwd in (
            (_store_listing_argv(listing), "store listing", copy),
            (_store_sort_argv(listing), "store listing sort", None),
        ):
            done = _checked(self.io, argv, f"{what} for {self.name}", cwd=cwd)
            if isinstance(done, Problem):
                return done
        pin = _listing_pin(self.io, listing)
        if isinstance(pin, Problem):
            return pin
        return Inventoried((), pin.bytes, pin)


def taking(
    io: Host,
    roots: Sequence[backupset.InventoryRoot],
    partial: str,
    previous: backupset.SetRef | None,
) -> tuple[TakingRoot, ...]:
    """Bind the registry rows to an in-flight set and its previous set, in registry order."""

    kinds = {
        backupset.RootKind.SNAPSHOT: _TakingSnapshot,
        backupset.RootKind.REPOSITORY: _TakingRepository,
        backupset.RootKind.STORE: _TakingStore,
    }
    return tuple(kinds[root.kind](io, root.name, root.source, root.exclusions, partial, previous) for root in roots)


@dataclass(frozen=True, slots=True)
class PushedRoot:
    """A root bound to its inventory and relative off-box location."""

    io: Host
    name: str
    entries: tuple[backupset.Entry, ...]
    label: str

    def check(self, *, verify_all: bool) -> Checked | Problem:
        raise NotImplementedError

    def arrived(self, previous: backupset.Manifest | None) -> int:
        """Bytes newly held by this root since the previous remote snapshot's set."""

        return 0

    def _sample(self, place: str, verify_all: bool) -> tuple[tuple[str, str], ...]:
        file_entries = tuple(
            entry for entry in self.entries if entry.kind == "f" and entry.sha256 is not None
        )
        paths = tuple(entry.path for entry in file_entries)
        selected = paths if verify_all else backupset.sample_paths(paths, percent=1, floor=1)
        entries_by_path = {entry.path: entry for entry in file_entries}
        sampled: list[tuple[str, str]] = []
        for path in selected:
            entry = entries_by_path[path]
            assert entry.sha256 is not None
            sampled.append((f"{place}/{path}", entry.sha256))
        return tuple(sampled)


def _relative_place(path: str) -> str:
    """Return *path* relative to staging: its place in a side directory and in the off-box copy."""

    return os.path.relpath(path, backupset.STAGING)


@dataclass(frozen=True, slots=True)
class _PushedSnapshot(PushedRoot):
    def check(self, *, verify_all: bool) -> Checked | Problem:
        place = os.path.join(_relative_place(backupset.files_dir(backupset.set_dir(self.label))), self.name)
        return Checked((), self._sample(place, verify_all))


@dataclass(frozen=True, slots=True)
class _PushedRepository(PushedRoot):
    source: str
    backup_label: str

    def check(self, *, verify_all: bool) -> Checked | Problem:
        place = _relative_place(self.source)
        repository_entries = {
            entry.path: entry
            for entry in self.entries
            if entry.kind == "f" and entry.sha256 is not None
        }
        # pgBackRest keeps its info files below the stanza directories; every one
        # of these is what a restore reads first, so the set must inventory them.
        special_paths = (
            f"backup/{pgbackrest.STANZA}/backup.info",
            f"archive/{pgbackrest.STANZA}/archive.info",
            f"backup/{pgbackrest.STANZA}/{self.backup_label}/backup.manifest",
        )
        mandatory: list[tuple[str, str]] = []
        for path in special_paths:
            entry = repository_entries.get(path)
            if entry is None or entry.sha256 is None:
                return Problem(f"the set's repository inventory lacks {path}", "")
            mandatory.append((f"{place}/{path}", entry.sha256))
        return Checked(tuple(mandatory), self._sample(place, verify_all))


@dataclass(frozen=True, slots=True)
class _PushedStore(PushedRoot):
    pin: backupset.ListingPin | None

    def check(self, *, verify_all: bool) -> Checked | Problem:
        if self.pin is None:
            return Checked((), ())
        listing = os.path.join(backupset.set_dir(self.label), self.pin.file)
        mandatory = ((_relative_place(listing), self.pin.sha256),)
        if verify_all:
            try:
                text = self.io.read_text(listing)
            except (OSError, UnicodeError) as exc:
                return Problem(f"store listing read for {self.name} failed: {exc}", "")
        else:
            selected = _checked(
                self.io, ["awk", "NR % 100 == 1", listing], f"store listing sample for {self.name}"
            )
            if isinstance(selected, Problem):
                return selected
            text = selected.stdout
        place = os.path.join(_relative_place(backupset.files_dir(backupset.set_dir(self.label))), self.name)
        sampled: list[tuple[str, str]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            name, separator, size = line.partition("\t")
            if not separator or _OBJECT_NAME.fullmatch(name) is None or not size.isdecimal():
                return Problem(f"store listing sample line {number} is malformed for {self.name}", "")
            sampled.append((f"{place}/{_object_relative(name)}", name))
        return Checked(mandatory, tuple(sampled))

    def arrived(self, previous: backupset.Manifest | None) -> int:
        if self.pin is None:
            return 0
        older = previous.listings.get(self.name) if previous is not None else None
        return max(0, self.pin.bytes - (older.bytes if older is not None else 0))


def pushed(
    io: Host,
    roots: Sequence[backupset.InventoryRoot],
    manifest: backupset.Manifest,
    label: str,
) -> tuple[PushedRoot, ...]:
    """Bind the sorted union of known and recorded roots to the off-box copy."""

    registry = {root.name: root for root in roots}
    bound: list[PushedRoot] = []
    for name in sorted(set(registry) | set(manifest.inventory)):
        row = registry.get(name)
        entries = manifest.inventory.get(name, ())
        if row is not None and row.kind is backupset.RootKind.STORE:
            bound.append(_PushedStore(io, name, entries, label, manifest.listings.get(name)))
        elif row is not None and row.kind is backupset.RootKind.REPOSITORY:
            bound.append(_PushedRepository(io, name, entries, label, row.source, manifest.pgbackrest_label))
        else:
            bound.append(_PushedSnapshot(io, name, entries, label))
    return tuple(bound)


# Owner groups are keyed by (uid, gid, is_link): a symlink is re-owned with
# ``chown -h`` so its referent — possibly outside the tree — is never touched,
# and it is never chmod-ed (a link has no mode of its own).
OwnerKey = tuple[int, int, bool]


def _validate_physical(
    io: Host,
    *,
    base: str,
    entries: Sequence[backupset.Entry],
    exact: bool = False,
) -> Problem | None:
    """Refuse unless every inventoried path exists physically with its declared kind.

    ``find`` never follows symlinks, so a path beneath a link is absent from
    the listing and a link where the manifest records a file or directory is
    listed as a link: both refuse before any ownership or mode is applied,
    which is what keeps a corrupt tree from steering ``chown`` or ``chmod``
    at a referent outside it.
    """

    try:
        listed = io.run(["find", base, "-printf", backupset.FIND_FORMAT])
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"cannot list {base}: {exc}", _physical_fix())
    if listed.returncode != 0:
        return Problem(f"cannot list {base}: {command_detail(listed)}", _physical_fix())
    try:
        kinds = {entry.path: entry.kind for entry in backupset.parse_find_listing(listed.stdout)}
    except ValueError as exc:
        return Problem(f"listing of {base} is malformed: {exc}", _physical_fix())
    mismatched = sum(1 for entry in entries if kinds.get(entry.path) != entry.kind)
    if mismatched:
        return Problem(
            f"{mismatched} inventoried path(s) under {base} are not what the manifest "
            "declares (a link in place of a file or directory, or a path beneath a link)",
            _physical_fix(),
        )
    if exact:
        # A snapshotted root is the manifest's set and nothing else: a path the
        # manifest never inventoried (an empty root's stray file included) is
        # refused before it can reach live state. The repository is never
        # judged this way — later backups legitimately add files there, and
        # pgBackRest's own verify covers it.
        inventoried = {entry.path for entry in entries}
        extraneous = sorted(
            path for path in kinds if path not in inventoried and path not in ("", backupset.ROOT_ENTRY)
        )
        if extraneous:
            return Problem(
                f"{len(extraneous)} path(s) under {base} are not in the manifest: {extraneous[0]}",
                _physical_fix(),
            )
    return None


@dataclass(slots=True)
class Claims:
    """Owner and mode claims gathered across roots and sets, applied once as one batch."""

    owners: dict[OwnerKey, list[str]] = field(default_factory=dict)
    modes: dict[int, list[str]] = field(default_factory=dict)
    entries_claimed: int = 0
    gideon_owned: int = 0

    @property
    def owner_paths(self) -> int:
        return sum(len(paths) for paths in self.owners.values())

    def add_owner(self, path: str, uid: int, gid: int, *, is_link: bool = False) -> None:
        self.owners.setdefault((uid, gid, is_link), []).append(path)

    def add_mode(self, path: str, mode: int) -> None:
        self.modes.setdefault(mode, []).append(path)

    def claim(self, path: str, entry: backupset.Entry, owner_map: backupset.OwnerMap) -> None:
        self.entries_claimed += 1
        self.gideon_owned += owner_map.matches(entry.uid, entry.gid)
        uid, gid = owner_map.map(entry.uid, entry.gid)
        is_link = entry.kind == "l"
        self.add_owner(path, uid, gid, is_link=is_link)
        if not is_link:
            self.add_mode(path, entry.mode)

    def merge(self, other: "Claims") -> None:
        for key, paths in other.owners.items():
            self.owners.setdefault(key, []).extend(paths)
        for mode, paths in other.modes.items():
            self.modes.setdefault(mode, []).extend(paths)
        self.entries_claimed += other.entries_claimed
        self.gideon_owned += other.gideon_owned

    def apply(self, io: Host, stage: str) -> Problem | None:
        for (uid, gid, is_link), paths in sorted(self.owners.items()):
            for start in range(0, len(paths), _CHUNK_SIZE):
                chunk = paths[start : start + _CHUNK_SIZE]
                result = run_stage(
                    io,
                    stage,
                    ["chown", *(("-h",) if is_link else ()), f"{uid}:{gid}", "--", *chunk],
                    f"re-owned {len(chunk)} path(s)",
                    "",
                )
                if not result.ok:
                    return Problem(result.detail, "")
        for mode, paths in sorted(self.modes.items()):
            for start in range(0, len(paths), _CHUNK_SIZE):
                chunk = paths[start : start + _CHUNK_SIZE]
                result = run_stage(
                    io,
                    stage,
                    ["chmod", f"{mode:04o}", "--", *chunk],
                    f"applied mode {mode:04o} to {len(chunk)} path(s)",
                    "",
                )
                if not result.ok:
                    return Problem(result.detail, "")
        return None


@dataclass(frozen=True, slots=True)
class LandedRoot:
    """A registry root bound to a fetched side directory before set discovery."""

    io: Host
    name: str
    source: str
    side: str

    def arrive(self, rendered_dir: PathLike) -> Problem | None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _LandedSnapshot(LandedRoot):
    """A fetched snapshot or store needs no arrival action."""

    def arrive(self, rendered_dir: PathLike) -> Problem | None:
        return None


@dataclass(frozen=True, slots=True)
class _LandedRepository(LandedRoot):
    """A fetched repository receives its container identity and default modes."""

    def arrive(self, rendered_dir: PathLike) -> Problem | None:
        identity, failure = pgbackrest.container_identity(self.io, rendered_dir)
        if failure is not None or identity is None:
            if failure is None:
                return Problem("postgres identity lookup failed", "")
            return Problem(failure.problem or "postgres identity lookup failed", failure.fix)
        uid, gid = identity
        repository = os.path.join(self.side, _relative_place(self.source))
        for argv, detail in (
            (["chown", "-R", "-h", f"{uid}:{gid}", repository], f"re-owned {repository}"),
            (
                ["find", repository, "-type", "d", "-exec", "chmod", "0750", "{}", "+"],
                f"applied pgBackRest directory modes under {repository}",
            ),
            (
                ["find", repository, "-type", "f", "-exec", "chmod", "0640", "{}", "+"],
                f"applied pgBackRest file modes under {repository}",
            ),
        ):
            result = run_stage(self.io, "fetch", argv, detail, "")
            if not result.ok:
                return Problem(result.detail, "")
        return None


def landed(io: Host, roots: Sequence[backupset.InventoryRoot], side: str) -> tuple[LandedRoot, ...]:
    """Bind registry roots to the fetched side directory in registry order."""

    return tuple(
        _LandedRepository(io, root.name, root.source, side)
        if root.kind is backupset.RootKind.REPOSITORY
        else _LandedSnapshot(io, root.name, root.source, side)
        for root in roots
    )


@dataclass(frozen=True, slots=True)
class FetchedRoot:
    """A root bound to a fetched set's inventory and copy."""

    io: Host
    name: str
    entries: tuple[backupset.Entry, ...]
    base: str
    manifest: backupset.Manifest

    def reown(self, gideon_ids: backupset.AccountIds) -> Claims | Problem:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _FetchedSnapshot(FetchedRoot):
    """A fetched snapshot contributes mapped owner and mode claims."""

    def reown(self, gideon_ids: backupset.AccountIds) -> Claims | Problem:
        failure = _validate_physical(self.io, base=self.base, entries=self.entries)
        if failure is not None:
            return failure
        claims = Claims()
        owner_map = backupset.OwnerMap(self.manifest.gideon_ids, gideon_ids)
        for entry in self.entries:
            claims.claim(os.path.join(self.base, entry.path), entry, owner_map)
        return claims


@dataclass(frozen=True, slots=True)
class _FetchedRepository(FetchedRoot):
    """A fetched repository restores modes differing from its defaults."""

    def reown(self, gideon_ids: backupset.AccountIds) -> Claims | Problem:
        failure = _validate_physical(self.io, base=self.base, entries=self.entries)
        if failure is not None:
            return failure
        overrides = Claims()
        for entry in self.entries:
            default = 0o750 if entry.kind == "d" else 0o640 if entry.kind == "f" else None
            if default is None or entry.mode == default:
                continue
            overrides.add_mode(os.path.join(self.base, entry.path), entry.mode)
        problem = overrides.apply(self.io, "fetch")
        return problem if problem is not None else Claims()


@dataclass(frozen=True, slots=True)
class _FetchedStore(FetchedRoot):
    """A fetched store copy needs one ownership pass and a listing claim."""

    pin: backupset.ListingPin
    set_path: str

    def reown(self, gideon_ids: backupset.AccountIds) -> Claims | Problem:
        del gideon_ids
        result = _checked(self.io, ["chown", "-R", "-h", "0:0", self.base], f"store reown for {self.name}")
        if isinstance(result, Problem):
            return result
        claims = Claims()
        listing = os.path.join(self.set_path, self.pin.file)
        claims.add_owner(listing, 0, 0)
        claims.add_mode(listing, 0o644)
        return claims


def fetched(
    io: Host,
    roots: Sequence[backupset.InventoryRoot],
    side: str,
    sets: Sequence[backupset.SetRef],
) -> tuple[FetchedRoot, ...]:
    """Bind repository roots first, then known snapshots and stores in each complete set."""

    registry = {root.name: root for root in roots}
    complete = tuple(ref for ref in sets if ref.complete and ref.manifest is not None)
    if not complete:
        return ()
    newest = complete[0]
    assert newest.manifest is not None
    bound: list[FetchedRoot] = []
    for row in roots:
        if row.kind is backupset.RootKind.REPOSITORY:
            bound.append(
                _FetchedRepository(
                    io,
                    row.name,
                    newest.manifest.inventory.get(row.name, ()),
                    os.path.join(side, _relative_place(row.source)),
                    newest.manifest,
                )
            )
    for ref in complete:
        assert ref.manifest is not None
        for name, entries in ref.manifest.inventory.items():
            matched = registry.get(name)
            if matched is not None and matched.kind is backupset.RootKind.SNAPSHOT:
                bound.append(
                    _FetchedSnapshot(
                        io,
                        name,
                        entries,
                        # The walk's base keeps its trailing slash, as the fetch has always listed it.
                        os.path.join(backupset.files_dir(ref.path), name, ""),
                        ref.manifest,
                    )
                )
        for row in roots:
            pin = ref.manifest.listings.get(row.name)
            if row.kind is backupset.RootKind.STORE and pin is not None:
                bound.append(
                    _FetchedStore(
                        io, row.name, (), os.path.join(backupset.files_dir(ref.path), row.name),
                        ref.manifest, pin, ref.path,
                    )
                )
    return tuple(bound)


def _verify_file_root(
    io: Host,
    root_name: str,
    entries: Sequence[backupset.Entry],
    base: str,
) -> Problem | None:
    lines = "".join(
        f"{entry.sha256}  {entry.path}\n"
        for entry in sorted(entries, key=lambda item: item.path)
        if entry.kind == "f" and entry.sha256 is not None
    )
    if any(entry.kind == "f" and entry.sha256 is None for entry in entries):
        return Problem(f"{root_name} contains a file without an inventory hash", "")
    if not lines:
        # A root with no files has no hash to check — the exact walk above has
        # already refused any stray path; sha256sum -c refuses an empty list
        # ("no properly formatted checksum lines found"), which is what
        # /data/registry is on a no-GPU host (the acceptance VM's first
        # rollback found this).
        return None
    try:
        result = io.run(
            ["sha256sum", "-c", "-"],
            input=lines,
            cwd=base,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Problem(f"checksum verification failed for {root_name}: {exc}", "")
    okay, failed = sshtarget.parse_check_output(result.stdout)
    del okay
    if failed:
        return Problem(f"checksum verification failed for {len(failed)} path(s) in {root_name}", "")
    if result.returncode != 0:
        return Problem(f"checksum verification failed for {root_name}: {command_detail(result)}", "")
    return None


@dataclass(frozen=True, slots=True)
class PutBack:
    """Restored root names, files-row clauses, and ownership claims."""

    restored: tuple[str, ...]
    clauses: tuple[str, ...]
    claims: Claims
    counts: Mapping[str, "StoreCounts"] = field(default_factory=dict)
    closing_lines: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoreCounts:
    """Objects added to the live store and objects the set could not supply."""

    added: int
    left_out: int


@dataclass(frozen=True, slots=True)
class HeldRoot:
    """A root bound to its live directory and copy in a complete set."""

    io: Host
    name: str
    source: str
    copy: str
    entries: tuple[backupset.Entry, ...]
    manifest: backupset.Manifest

    def verify(self) -> Problem | None:
        raise NotImplementedError

    def prove(self, rendered_dir: PathLike) -> Problem | None:
        raise NotImplementedError

    def put_back(self, host_ids: backupset.AccountIds) -> PutBack | Problem:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _HeldSnapshot(HeldRoot):
    """A snapshot verifies its copy and optionally restores its live directory."""

    exclusions: tuple[str, ...]
    restore_in_place: bool

    def verify(self) -> Problem | None:
        walked = _validate_physical(self.io, base=self.copy, entries=self.entries, exact=True)
        if walked is not None:
            return walked
        return _verify_file_root(self.io, self.name, self.entries, self.copy)

    def prove(self, rendered_dir: PathLike) -> Problem | None:
        return None

    def put_back(self, host_ids: backupset.AccountIds) -> PutBack | Problem:
        claims = Claims()
        if not self.restore_in_place:
            return PutBack(
                (),
                (f"the {self.name} copy stays in {self.copy} (clone the tag the manifest names)",),
                claims,
            )
        # rsync applies the source directory's own attributes to the live
        # directory; a set that records no "." entry for the root (one made
        # before the entry existed) must not hand the copy's owner to it, so
        # the live directory's owner and mode are taken now and put back.
        keep: tuple[int, int, int] | None = None
        if not any(entry.path == backupset.ROOT_ENTRY for entry in self.entries):
            try:
                details = self.io.stat(self.source)
                keep = (details.st_uid, details.st_gid, stat.S_IMODE(details.st_mode))
            except OSError:
                keep = None
        argv = ["rsync", "-a", "--delete"]
        argv.extend(f"--exclude={pattern}" for pattern in self.exclusions)
        argv.extend([self.copy.rstrip("/") + "/", self.source.rstrip("/") + "/"])
        result = run_stage(self.io, "files", argv, f"restored {self.name}", "")
        if not result.ok:
            return Problem(result.detail, "")
        if keep is not None:
            uid, gid, mode = keep
            claims.add_owner(self.source, uid, gid)
            claims.add_mode(self.source, mode)
        failure = _validate_physical(self.io, base=self.source, entries=self.entries)
        if failure is not None:
            return failure
        owner_map = backupset.OwnerMap(self.manifest.gideon_ids, host_ids)
        for entry in self.entries:
            claims.claim(os.path.join(self.source, entry.path), entry, owner_map)
        return PutBack((self.name,), (), claims)


@dataclass(frozen=True, slots=True)
class _HeldRepository(HeldRoot):
    """A repository verifies immutable hashes and pgBackRest's own record."""

    def verify(self) -> Problem | None:
        # A later backup has legitimately rewritten the info files since
        # this set was made; pgBackRest's verify below proves them.
        entries = tuple(
            entry
            for entry in self.entries
            if backupset.MUTABLE_REPOSITORY_FILE.fullmatch(entry.path.rsplit("/", 1)[-1]) is None
        )
        return _verify_file_root(self.io, self.name, entries, self.copy)

    def prove(self, rendered_dir: PathLike) -> Problem | None:
        repository = None if self.copy == self.source else self.copy
        problem = pgbackrest.verify(
            self.io,
            pgbackrest.verify_argv(rendered_dir, repository=repository),
        )
        return Problem(problem, "") if problem is not None else None

    def put_back(self, host_ids: backupset.AccountIds) -> PutBack | Problem:
        return PutBack((), (), Claims())


@dataclass(frozen=True, slots=True)
class _HeldStore(HeldRoot):
    """A store verifies its listing and restores only missing, intact objects."""

    pin: backupset.ListingPin | None
    later: backupset.SetRef | None = None

    def verify(self) -> Problem | None:
        for copy, pin in self._listings():
            problem = self._verify_listing(copy, pin)
            if problem is not None:
                return problem
        return None

    def _listings(self) -> tuple[tuple[str, backupset.ListingPin], ...]:
        listings: list[tuple[str, backupset.ListingPin]] = []
        if self.pin is not None:
            listings.append((self.copy, self.pin))
        if self.later is not None and self.later.manifest is not None:
            pin = self.later.manifest.listings.get(self.name)
            if pin is not None:
                copy = os.path.join(backupset.files_dir(self.later.path), self.name)
                listings.append((copy, pin))
        return tuple(listings)

    def _verify_listing(self, copy: str, pin: backupset.ListingPin) -> Problem | None:
        listing = os.path.join(os.path.dirname(os.path.dirname(copy)), pin.file)
        try:
            size = self.io.stat(listing).st_size
        except OSError as exc:
            return Problem(f"store listing for {self.name} cannot be read: {exc}", _physical_fix())
        hashed = _checked(self.io, ["sha256sum", listing], f"store listing hash for {self.name}")
        if isinstance(hashed, Problem):
            return Problem(hashed.problem, _physical_fix())
        try:
            digest = backupset.parse_sha256sum(hashed.stdout)[listing]
        except (KeyError, ValueError) as exc:
            return Problem(f"store listing hash for {self.name} is malformed: {exc}", _physical_fix())
        if size != pin.size or digest != pin.sha256:
            return Problem(f"store listing for {self.name} does not match its manifest pin", _physical_fix())
        return None

    def prove(self, rendered_dir: PathLike) -> Problem | None:
        return None

    def put_back(self, host_ids: backupset.AccountIds) -> PutBack | Problem:
        del host_ids
        listings = self._listings()
        if not listings:
            return PutBack((), (f"{self.name}: the set predates this root; the live store is left as it is",), Claims())
        try:
            if not self.io.exists(self.source):
                return Problem(f"content-addressed store root is missing: {self.source}", cas.PROVISION_FIX)
        except OSError as exc:
            return Problem(f"store root check for {self.name} failed: {exc}", "")
        live_listing = f"{backupset.STAGING}.live-{self.name}.listing"
        added = 0
        missing: set[str] = set()
        for copy, pin in listings:
            try:
                # A leftover of a killed restore goes first; each pass makes a
                # fresh live listing so it sees objects added by the prior pass.
                self.io.unlink(live_listing, missing_ok=True)
            except OSError as exc:
                return Problem(f"store listing cleanup for {self.name} failed: {exc}", "")
            try:
                needed = self._needed(live_listing, copy, pin)
            finally:
                # The next restore removes any scratch file left by this one.
                with suppress(OSError):
                    self.io.unlink(live_listing, missing_ok=True)
            if isinstance(needed, Problem):
                return needed
            checked = self._hash_needed(needed, copy)
            if isinstance(checked, Problem):
                return checked
            passing, failed = checked
            missing.update(failed)
            if passing:
                argv = _store_copy_argv(
                    copy, self.source, (),
                    f"--chmod=D{cas.DIRECTORY_MODE:o},F{cas.OBJECT_MODE:o}",
                    "--ignore-existing", "--files-from=-",
                )
                transferred = _transferred(
                    self.io, argv, f"store put-back for {self.name}",
                    input="".join(f"{_object_relative(name)}\n" for name in passing),
                )
                if isinstance(transferred, Problem):
                    return transferred
                added += transferred
                missing.difference_update(passing)
        left_out = len(missing)
        detail = NOTHING_LEFT_OUT_DETAIL if left_out == 0 else f"{left_out} left out"
        suffix = f" (with {self.later.label})" if self.later is not None else ""
        closing = () if left_out == 0 else (
            f"Store: {left_out} object(s) the set could not supply whole were left out: "
            f"{', '.join(sorted(missing)[:_LEFT_OUT_NAMED])}; write each object's bytes again from their source "
            "(the whole list: docs/runbooks/backup-restore.md §4)",
        )
        return PutBack(
            (), (f"{self.name}: added {added} object(s), {detail}{suffix}",), Claims(),
            {self.name: StoreCounts(added, left_out)}, closing,
        )

    def _needed(self, live_listing: str, copy: str, pin: backupset.ListingPin) -> list[str] | Problem:
        """The names the set lists and the live store lacks, matched by name alone."""

        listing = os.path.join(os.path.dirname(os.path.dirname(copy)), pin.file)
        joined: CompletedText | None = None
        for argv, what, cwd in (
            (_store_listing_argv(live_listing), "live store listing", self.source),
            (_store_sort_argv(live_listing), "live store listing sort", None),
            (["env", "LC_ALL=C", "join", "-t", "\t", "-j", "1", "-v", "1", listing, live_listing],
             "store listing join", None),
        ):
            result = _checked(self.io, argv, f"{what} for {self.name}", cwd=cwd)
            if isinstance(result, Problem):
                return result
            joined = result
        assert joined is not None
        needed: list[str] = []
        for line in joined.stdout.splitlines():
            name, separator, size = line.partition("\t")
            if not separator or _OBJECT_NAME.fullmatch(name) is None or not size.isdecimal():
                return Problem(f"store listing join for {self.name} returned a malformed line", "")
            needed.append(name)
        return needed

    def _hash_needed(self, needed: Sequence[str], copy: str) -> tuple[list[str], list[str]] | Problem:
        """Split the needed names into those whose bytes in the set's copy hash to them, and the rest."""

        passing: list[str] = []
        failed: list[str] = []
        for start in range(0, len(needed), _HASH_BATCH):
            paths = {_object_relative(name): name for name in needed[start : start + _HASH_BATCH]}
            checks = "".join(f"{name}  {path}\n" for path, name in paths.items())
            try:
                checked = self.io.run(["sha256sum", "-c", "-"], input=checks, cwd=copy)
            except (OSError, subprocess.SubprocessError) as exc:
                return Problem(f"store object check for {self.name} failed: {exc}", "")
            okay_paths, failed_paths = sshtarget.parse_check_output(checked.stdout)
            # sha256sum exits 1 whenever a line fails, so the exit alone is no
            # refusal: a report that does not account for every line is.
            if (
                checked.returncode not in (0, 1)
                or set(okay_paths + failed_paths) != set(paths)
                or len(okay_paths) + len(failed_paths) != len(paths)
                or bool(failed_paths) != (checked.returncode == 1)
            ):
                return Problem(f"store object check for {self.name} failed: {command_detail(checked)}", "")
            passing.extend(paths[path] for path in okay_paths)
            failed.extend(paths[path] for path in failed_paths)
        return passing, failed


def held(
    io: Host,
    roots: Sequence[backupset.InventoryRoot],
    set_ref: backupset.SetRef,
    later: backupset.SetRef | None = None,
) -> tuple[HeldRoot, ...]:
    """Bind registry roots in order to their places in a complete set."""

    if set_ref.manifest is None:
        raise ValueError("held set has no manifest")
    staging = os.path.dirname(os.path.dirname(set_ref.path))
    bound: list[HeldRoot] = []
    for row in roots:
        entries = set_ref.manifest.inventory.get(row.name, ())
        if row.kind is backupset.RootKind.SNAPSHOT:
            bound.append(
                _HeldSnapshot(
                    io,
                    row.name,
                    row.source,
                    os.path.join(backupset.files_dir(set_ref.path), row.name),
                    entries,
                    set_ref.manifest,
                    row.exclusions,
                    row.restore_in_place,
                )
            )
        elif row.kind is backupset.RootKind.STORE:
            bound.append(
                _HeldStore(
                    io, row.name, row.source,
                    os.path.join(backupset.files_dir(set_ref.path), row.name), entries,
                    set_ref.manifest, set_ref.manifest.listings.get(row.name), later,
                )
            )
        else:
            copy = (
                row.source
                if staging == backupset.STAGING
                else os.path.join(staging, _relative_place(row.source))
            )
            bound.append(
                _HeldRepository(
                    io,
                    row.name,
                    row.source,
                    copy,
                    entries,
                    set_ref.manifest,
                )
            )
    return tuple(bound)


def carrying(held_roots: Sequence[HeldRoot], source: str) -> HeldRoot:
    """Find the held root carrying a live directory."""

    for root in held_roots:
        if root.source == source:
            return root
    raise LookupError(source)
