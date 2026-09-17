"""The local backup-set layout and its pure data model (spec §19.1)."""

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gideon.host import nogpu
from gideon.host.render import ARTIFACTS
from gideon.host.render.pgbackrest import REPOSITORY_PATH
from gideon.host.report import Problem
from gideon.host.steps.site_dirs import AGE_IDENTITY_PATH
from gideon.host.sysio import Host, PathLike

STAGING: Final = "/data/backup-staging"
SETS_DIR: Final = f"{STAGING}/sets"
MANIFEST_NAME: Final = "manifest.json"
TARBALL_NAME: Final = "secrets.tar.age"
FILES_DIR: Final = "files"
PUSH_RECORD_NAME: Final = "push.json"
PARTIAL_SUFFIX: Final = ".partial"
# The system account provision creates for the host services; the one name
# a restore resolves on the host it runs on.
SERVICE_ACCOUNT: Final = "gideon"
# find expands these escapes itself; an argv string cannot carry a NUL, so the
# format travels as the two-character sequences and the listing arrives with
# real tabs and NULs.
FIND_FORMAT: Final = r"%y\t%s\t%U\t%G\t%m\t%T@\t%P\0"


def set_dir(label: str) -> str:
    """Return the final staging directory for *label*."""

    return f"{SETS_DIR}/{label}"


def partial_dir(label: str) -> str:
    """Return the in-flight staging directory for *label*."""

    return f"{set_dir(label)}{PARTIAL_SUFFIX}"


@dataclass(frozen=True, slots=True)
class InventoryRoot:
    """One root included in a backup set's inventory."""

    name: str
    source: str
    exclusions: tuple[str, ...]
    snapshotted: bool
    restore_in_place: bool


# Derived tool state under a checkout: never release content, never restored.
CHECKOUT_EXCLUSIONS: Final = (
    ".venv/",
    "__pycache__/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".pytest_cache/",
)


def etc_gideon_exclusions() -> tuple[str, ...]:
    """The state that a backup set must not carry between hosts.

    A secret-flagged artifact (the frontend's env file, SearXNG's) carries
    secret values in plaintext; apply re-renders it from the secrets the age
    tarball holds, so the set never carries it.  The no-GPU and build-box
    markers are host state converged by provision, never carried in a set or
    restored: a set from a VM restored on a GPU box, or the reverse, can never
    flip the host's mode. The box identity is host state of the box that made
    the set — a set carries no key that opens it, and a rebuilt box takes the
    office identity.
    """

    return (
        "secrets/",
        *(f"rendered/{artifact.relative_path}" for artifact in ARTIFACTS if artifact.secret),
        os.fspath(nogpu.NO_GPU_PATH.relative_to("/etc/gideon")),
        os.fspath(nogpu.BUILD_BOX_PATH.relative_to("/etc/gideon")),
        os.fspath(AGE_IDENTITY_PATH.relative_to("/etc/gideon")),
    )


def inventory_roots(checkout: str) -> tuple[InventoryRoot, ...]:
    """Return the ordered five-root inventory registry."""

    # A root a container writes under an id of its image's own would carry
    # its ownership policy here, beside restore_in_place; none does today.
    return (
        InventoryRoot("etc-gideon", "/etc/gideon", etc_gideon_exclusions(), True, True),
        InventoryRoot("checkout", checkout, CHECKOUT_EXCLUSIONS, True, False),
        InventoryRoot("data-registry", "/data/registry", (), True, True),
        InventoryRoot(
            "data-bulk-openwebui", "/data/bulk/openwebui", (), True, True
        ),
        InventoryRoot("pgbackrest", REPOSITORY_PATH, (), False, False),
    )


class Kind(StrEnum):
    """The three kinds of local backup set."""

    NIGHTLY = "nightly"
    LABELLED = "labelled"
    PRE_RESTORE = "pre-restore"


_NIGHTLY_LABEL = r"\d{8}T\d{6}Z"
LABEL: Final = re.compile(
    rf"^(?:{_NIGHTLY_LABEL}|pre-restore-{_NIGHTLY_LABEL}|pre-[A-Za-z0-9._-]+)$"
)
_LABEL_FIX: Final = (
    "Use a nightly YYYYMMDDTHHMMSSZ label, pre-restore-<timestamp>, or an "
    "operator label matching pre-[A-Za-z0-9._-]+."
)


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _utc(value: datetime, field: str) -> datetime:
    return _aware(value, field).astimezone(UTC)


def nightly_label(now: datetime) -> str:
    """Render an aware instant as the basic UTC nightly label."""

    return _utc(now, "now").strftime("%Y%m%dT%H%M%SZ")


def pre_restore_label(now: datetime) -> str:
    """Render an aware instant as a pre-restore label."""

    return f"pre-restore-{nightly_label(now)}"


def validate_label(label: str) -> Problem | None:
    """Return the problem when *label* is outside the label grammar."""

    if isinstance(label, str) and LABEL.fullmatch(label) is not None:
        return None
    return Problem("Backup label is invalid.", _LABEL_FIX)


def kind_of(label: str) -> Kind | None:
    """Classify a valid label, or return ``None`` for an invalid label."""

    if validate_label(label) is not None:
        return None
    if label.startswith("pre-restore-"):
        return Kind.PRE_RESTORE
    if label.startswith("pre-"):
        return Kind.LABELLED
    return Kind.NIGHTLY


@dataclass(frozen=True, slots=True)
class AccountIds:
    """The numeric uid and gid of the ``gideon`` account."""

    uid: int
    gid: int

    def __post_init__(self) -> None:
        for field in ("uid", "gid"):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"AccountIds {field} must be an integer")
            if value < 0:
                raise ValueError(f"AccountIds {field} must be non-negative")


def gideon_account_ids(host: Host) -> AccountIds | None:
    """The ids of the ``gideon`` account through the seam; ``None`` when absent or malformed.

    The one place the account is resolved: ``backup run`` records the pair
    in the manifest it writes, and ``restore`` reads the restoring host's
    own to map what the making host's ``gideon`` owned onto it.
    """

    result = host.run(["getent", "passwd", SERVICE_ACCOUNT])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if len(fields) >= 7 and fields[0] == SERVICE_ACCOUNT:
            try:
                return AccountIds(int(fields[2]), int(fields[3]))
            except (TypeError, ValueError):
                return None
    return None


EntryKind = Literal["f", "d", "l"]
_ENTRY_KINDS: Final = frozenset({"f", "d", "l"})
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True, slots=True)
class Entry:
    """Metadata for one inventoried path relative to its inventory root."""

    path: str
    kind: EntryKind
    size: int
    uid: int
    gid: int
    mode: int
    mtime: float
    sha256: str | None


def _relative_path(path: str) -> bool:
    if not path or path.startswith("/"):
        return False
    return ".." not in PurePosixPath(path).parts


ROOT_ENTRY: Final = "."


def parse_find_listing(text: str) -> tuple[Entry, ...]:
    """Parse NUL-separated ``find -printf`` output.

    ``find`` emits the root itself with an empty ``%P``; it is recorded as the
    entry ``.`` so the root directory's own owner and mode travel with the set
    — a restore applies them to the directory it rsyncs into.
    """

    entries: list[Entry] = []
    for record_number, record in enumerate(text.split("\0"), start=1):
        if not record:
            continue
        fields = record.split("\t", 6)
        if len(fields) != 7:
            raise ValueError(
                f"find listing record {record_number} has the wrong field count"
            )
        kind_text, size_text, uid_text, gid_text, mode_text, mtime_text, path = fields
        if not path:
            path = ROOT_ENTRY
        if kind_text not in _ENTRY_KINDS:
            raise ValueError(f"find listing record {record_number} has an invalid type")
        if not _relative_path(path):
            raise ValueError(
                f"find listing record {record_number} has a non-relative path"
            )
        try:
            size = int(size_text, 10)
            uid = int(uid_text, 10)
            gid = int(gid_text, 10)
            mode = int(mode_text, 8)
            mtime = float(mtime_text)
        except ValueError as exc:
            raise ValueError(
                f"find listing record {record_number} has invalid metadata"
            ) from exc
        if size < 0 or uid < 0 or gid < 0 or not 0 <= mode <= 0o7777:
            raise ValueError(
                f"find listing record {record_number} has invalid metadata"
            )
        if not math.isfinite(mtime):
            raise ValueError(
                f"find listing record {record_number} has an invalid mtime"
            )
        entries.append(
            Entry(path, cast(EntryKind, kind_text), size, uid, gid, mode, mtime, None)
        )
    return tuple(entries)


def _unescape_checksum_path(path: str) -> str:
    marker = "\x00"
    return path.replace("\\\\", marker).replace("\\n", "\n").replace(marker, "\\")


def parse_sha256sum(text: str) -> Mapping[str, str]:
    """Parse ordinary and GNU leading-backslash ``sha256sum`` output."""

    hashes: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        escaped = line.startswith("\\")
        if escaped:
            line = line[1:]
        digest, separator, path = line.partition("  ")
        if not separator:
            digest, separator, path = line.partition(" ")
        if not separator or _SHA256.fullmatch(digest) is None or not path:
            raise ValueError(f"sha256sum line {line_number} is malformed")
        if escaped:
            path = _unescape_checksum_path(path)
        hashes[path] = digest.lower()
    return hashes


_REHASH_FIX: Final = "Re-run sudo python3 -m gideon backup run to regenerate the inventory hashes."


def merge_hashes(
    entries: Sequence[Entry], hashes: Mapping[str, str]
) -> tuple[Entry, ...] | Problem:
    """Attach file hashes, refusing an inventory with an unhashed file."""

    merged: list[Entry] = []
    missing: list[str] = []
    for entry in entries:
        if entry.kind != "f":
            merged.append(entry)
            continue
        digest = hashes.get(entry.path)
        if digest is None:
            missing.append(entry.path)
            merged.append(entry)
            continue
        if _SHA256.fullmatch(digest) is None:
            return Problem(
                f"Hash for file entry {entry.path} is not 64-character hexadecimal.",
                _REHASH_FIX,
            )
        merged.append(replace(entry, sha256=digest.lower()))
    if missing:
        paths = ", ".join(missing)
        return Problem(f"File entries are missing sha256 hashes: {paths}.", _REHASH_FIX)
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class LinkVerdict:
    """The deterministic hard-link sample result."""

    sampled: int
    linked: int


def _entry_json(entry: Entry) -> dict[str, object]:
    return {
        "gid": entry.gid,
        "kind": entry.kind,
        "mode": entry.mode,
        "mtime": entry.mtime,
        "path": entry.path,
        "sha256": entry.sha256,
        "size": entry.size,
        "uid": entry.uid,
    }


_MANIFEST_FIX: Final = (
    "Write a complete manifest.json from backup run, then retry."
)
_PUSH_RECORD_FIX: Final = "Write a complete push.json from backup push, then retry."
_MISSING = object()


class _ParseError(Exception):
    """A document field failed validation; carries the operator-facing problem."""

    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.problem)
        self.problem = problem


def _field_problem(prefix: str, detail: str, fix: str = _MANIFEST_FIX) -> Problem:
    return Problem(f"Field '{prefix}' {detail}.", fix)


def _required(document: Mapping[str, object], field: str, fix: str) -> object:
    value = document.get(field, _MISSING)
    if value is _MISSING:
        raise _ParseError(_field_problem(field, "is missing", fix))
    return value


def _string_field(
    document: Mapping[str, object], field: str, *, fix: str = _MANIFEST_FIX
) -> str:
    value = _required(document, field, fix)
    if not isinstance(value, str) or not value:
        raise _ParseError(_field_problem(field, "must be a non-empty string", fix))
    return value


def _datetime_field(
    document: Mapping[str, object], field: str, *, fix: str = _MANIFEST_FIX
) -> datetime:
    value = _required(document, field, fix)
    if not isinstance(value, str):
        raise _ParseError(_field_problem(field, "must be an ISO 8601 string", fix))
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _ParseError(_field_problem(field, "must be an ISO 8601 datetime", fix)) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _ParseError(_field_problem(field, "must include a timezone offset", fix))
    return parsed


def _integer(value: object, field: str, *, nonnegative: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _ParseError(_field_problem(field, "must be an integer"))
    if nonnegative and value < 0:
        raise _ParseError(_field_problem(field, "must be a non-negative integer"))
    return value


def _entry_from_json(value: object, field: str) -> Entry:
    if not isinstance(value, Mapping):
        raise _ParseError(_field_problem(field, "must be an object"))
    path_value = value.get("path", _MISSING)
    if not isinstance(path_value, str) or not _relative_path(path_value):
        raise _ParseError(_field_problem(f"{field}.path", "must be a relative path"))
    kind_value = value.get("kind", _MISSING)
    if not isinstance(kind_value, str) or kind_value not in _ENTRY_KINDS:
        raise _ParseError(_field_problem(f"{field}.kind", "must be f, d, or l"))
    size = _integer(value.get("size", _MISSING), f"{field}.size", nonnegative=True)
    uid = _integer(value.get("uid", _MISSING), f"{field}.uid", nonnegative=True)
    gid = _integer(value.get("gid", _MISSING), f"{field}.gid", nonnegative=True)
    mode = _integer(value.get("mode", _MISSING), f"{field}.mode", nonnegative=True)
    if mode > 0o7777:
        raise _ParseError(_field_problem(f"{field}.mode", "must contain permission bits only"))
    mtime_value = value.get("mtime", _MISSING)
    if isinstance(mtime_value, bool) or not isinstance(mtime_value, (int, float)):
        raise _ParseError(_field_problem(f"{field}.mtime", "must be a number"))
    mtime = float(mtime_value)
    if not math.isfinite(mtime):
        raise _ParseError(_field_problem(f"{field}.mtime", "must be finite"))
    sha_value = value.get("sha256", _MISSING)
    if sha_value is _MISSING:
        raise _ParseError(_field_problem(f"{field}.sha256", "is missing"))
    if sha_value is not None and (
        not isinstance(sha_value, str) or _SHA256.fullmatch(sha_value) is None
    ):
        raise _ParseError(
            _field_problem(f"{field}.sha256", "must be null or hexadecimal")
        )
    if kind_value == "f" and sha_value is None:
        raise _ParseError(_field_problem(f"{field}.sha256", "is required for a file"))
    return Entry(
        path_value,
        cast(EntryKind, kind_value),
        size,
        uid,
        gid,
        mode,
        mtime,
        None if sha_value is None else sha_value.lower(),
    )


def _mapping_field(value: object, field: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise _ParseError(_field_problem(field, "must be an object"))
    return value


@dataclass(frozen=True, slots=True)
class Manifest:
    """The complete, immutable description of one local backup set."""

    version: int
    label: str
    kind: Kind
    started: datetime
    finished: datetime
    release: str
    checkout: str
    commit: str
    hostname: str
    previous_label: str | None
    pgbackrest_label: str
    pgbackrest_type: str
    archive_through: datetime
    row_counts: Mapping[str, Mapping[str, int]]
    inventory: Mapping[str, tuple[Entry, ...]]
    tarball_sha256: str
    recipients: tuple[str, ...]
    hard_links: LinkVerdict
    secrets_fingerprint: str
    gideon_ids: AccountIds | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version != 1
        ):
            raise ValueError("Manifest version must be 1")
        if not isinstance(self.kind, Kind):
            raise TypeError("Manifest kind must be a Kind")
        if not isinstance(self.recipients, tuple) or not self.recipients or any(
            not isinstance(recipient, str) or not recipient
            for recipient in self.recipients
        ):
            raise ValueError(
                "Manifest recipients must be a non-empty tuple of non-empty strings"
            )
        if self.gideon_ids is not None and not isinstance(self.gideon_ids, AccountIds):
            raise TypeError("Manifest gideon_ids must be AccountIds or None")
        for field in ("started", "finished", "archive_through"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))

    def to_json(self) -> str:
        """Serialize this manifest deterministically as indented JSON."""

        document: dict[str, object] = {
            "archive_through": _datetime_json(self.archive_through),
            "checkout": self.checkout,
            "commit": self.commit,
            "finished": _datetime_json(self.finished),
            "hard_links": {
                "linked": self.hard_links.linked,
                "sampled": self.hard_links.sampled,
            },
            "hostname": self.hostname,
            "inventory": {
                name: [_entry_json(entry) for entry in entries]
                for name, entries in self.inventory.items()
            },
            "kind": self.kind.value,
            "label": self.label,
            "pgbackrest_label": self.pgbackrest_label,
            "pgbackrest_type": self.pgbackrest_type,
            "previous_label": self.previous_label,
            # Keep the singular for a previous tree reading this set after rollback.
            "recipient": self.recipients[0],
            "recipients": list(self.recipients),
            "release": self.release,
            "row_counts": {
                database: dict(counts)
                for database, counts in self.row_counts.items()
            },
            "secrets_fingerprint": self.secrets_fingerprint,
            "started": _datetime_json(self.started),
            "tarball_sha256": self.tarball_sha256,
            "version": self.version,
        }
        if self.gideon_ids is not None:
            document["gideon_ids"] = {
                "gid": self.gideon_ids.gid,
                "uid": self.gideon_ids.uid,
            }
        return json.dumps(document, indent=1, sort_keys=True) + "\n"


def _datetime_json(value: datetime) -> str:
    return _utc(value, "datetime").isoformat()


def _parse_manifest_document(document: object) -> Manifest:
    if not isinstance(document, Mapping):
        raise _ParseError(_field_problem("manifest", "must be an object"))
    version_value = _required(document, "version", _MANIFEST_FIX)
    if not isinstance(version_value, int) or isinstance(version_value, bool):
        raise _ParseError(_field_problem("version", "must be the integer 1"))
    if version_value != 1:
        raise _ParseError(_field_problem("version", "must be 1"))

    label = _string_field(document, "label")
    label_problem = validate_label(label)
    if label_problem is not None:
        raise _ParseError(_field_problem("label", "must use the backup label grammar"))
    kind_value = _string_field(document, "kind")
    try:
        kind = Kind(kind_value)
    except ValueError as exc:
        raise _ParseError(_field_problem("kind", "must be nightly, labelled, or pre-restore")) from exc

    started = _datetime_field(document, "started")
    finished = _datetime_field(document, "finished")
    release = _string_field(document, "release")
    checkout = _string_field(document, "checkout")
    commit = _string_field(document, "commit")
    hostname = _string_field(document, "hostname")
    previous_value = _required(document, "previous_label", _MANIFEST_FIX)
    if previous_value is not None and not isinstance(previous_value, str):
        raise _ParseError(_field_problem("previous_label", "must be a string or null"))
    if isinstance(previous_value, str) and validate_label(previous_value) is not None:
        raise _ParseError(_field_problem("previous_label", "must use the backup label grammar"))
    pgbackrest_label = _string_field(document, "pgbackrest_label")
    pgbackrest_type = _string_field(document, "pgbackrest_type")
    archive_through = _datetime_field(document, "archive_through")

    row_counts_value = _mapping_field(_required(document, "row_counts", _MANIFEST_FIX), "row_counts")
    row_counts: dict[str, Mapping[str, int]] = {}
    for database, counts_value in row_counts_value.items():
        if not isinstance(database, str):
            raise _ParseError(_field_problem("row_counts", "database names must be strings"))
        counts_mapping = _mapping_field(counts_value, f"row_counts.{database}")
        counts: dict[str, int] = {}
        for table, count_value in counts_mapping.items():
            if not isinstance(table, str):
                raise _ParseError(_field_problem(f"row_counts.{database}", "table names must be strings"))
            counts[table] = _integer(
                count_value, f"row_counts.{database}.{table}", nonnegative=True
            )
        row_counts[database] = counts

    inventory_value = _mapping_field(_required(document, "inventory", _MANIFEST_FIX), "inventory")
    inventory: dict[str, tuple[Entry, ...]] = {}
    for root, entries_value in inventory_value.items():
        if not isinstance(root, str):
            raise _ParseError(_field_problem("inventory", "root names must be strings"))
        if not isinstance(entries_value, list):
            raise _ParseError(_field_problem(f"inventory.{root}", "must be an array"))
        inventory[root] = tuple(
            _entry_from_json(value, f"inventory.{root}[{index}]")
            for index, value in enumerate(entries_value)
        )

    tarball_sha256 = _string_field(document, "tarball_sha256")
    if _SHA256.fullmatch(tarball_sha256) is None:
        raise _ParseError(_field_problem("tarball_sha256", "must be hexadecimal"))
    recipient = _string_field(document, "recipient")
    recipients_value = document.get("recipients", _MISSING)
    if recipients_value is _MISSING:
        recipients = (recipient,)
    else:
        if not isinstance(recipients_value, list) or not recipients_value or any(
            not isinstance(value, str) or not value for value in recipients_value
        ):
            raise _ParseError(
                _field_problem(
                    "recipients",
                    "must be a non-empty array of non-empty strings",
                )
            )
        recipients = tuple(recipients_value)
        if recipients[0] != recipient:
            raise _ParseError(
                _field_problem("recipients", "must begin with recipient")
            )
    gideon_ids_value = document.get("gideon_ids", _MISSING)
    if gideon_ids_value is _MISSING:
        gideon_ids = None
    else:
        gideon_ids_mapping = _mapping_field(gideon_ids_value, "gideon_ids")
        gideon_ids = AccountIds(
            _integer(
                gideon_ids_mapping.get("uid", _MISSING),
                "gideon_ids.uid",
                nonnegative=True,
            ),
            _integer(
                gideon_ids_mapping.get("gid", _MISSING),
                "gideon_ids.gid",
                nonnegative=True,
            ),
        )
    hard_links_value = _mapping_field(
        _required(document, "hard_links", _MANIFEST_FIX), "hard_links"
    )
    sampled = _integer(
        hard_links_value.get("sampled", _MISSING), "hard_links.sampled", nonnegative=True
    )
    linked = _integer(
        hard_links_value.get("linked", _MISSING), "hard_links.linked", nonnegative=True
    )
    if linked > sampled:
        raise _ParseError(_field_problem("hard_links.linked", "must not exceed sampled"))
    secrets_fingerprint = _string_field(document, "secrets_fingerprint")
    if _SHA256.fullmatch(secrets_fingerprint) is None:
        raise _ParseError(_field_problem("secrets_fingerprint", "must be hexadecimal"))
    try:
        return Manifest(
            version_value,
            label,
            kind,
            started,
            finished,
            release,
            checkout,
            commit,
            hostname,
            previous_value,
            pgbackrest_label,
            pgbackrest_type,
            archive_through,
            row_counts,
            inventory,
            tarball_sha256.lower(),
            recipients,
            LinkVerdict(sampled, linked),
            secrets_fingerprint.lower(),
            gideon_ids,
        )
    except (TypeError, ValueError) as exc:
        raise _ParseError(_field_problem("manifest", str(exc))) from exc


def parse_manifest(text: str) -> Manifest | Problem:
    """Parse a manifest, returning a field-specific problem on failure."""

    try:
        document = json.loads(text)
        return _parse_manifest_document(document)
    except json.JSONDecodeError as exc:
        return Problem(f"Manifest JSON is malformed: {exc.msg}.", _MANIFEST_FIX)
    except _ParseError as exc:
        return exc.problem
    except (TypeError, ValueError) as exc:
        # A constructor guard (a naive datetime, a wrong kind) rather than a
        # field check: still a refusal, never a traceback.
        return Problem(f"Manifest is invalid: {exc}.", _MANIFEST_FIX)


@dataclass(frozen=True, slots=True)
class OwnerMap:
    """Map recorded owner ids to the restoring host's ``gideon`` ids."""

    recorded: AccountIds | None
    host: AccountIds

    def map(self, uid: int, gid: int) -> tuple[int, int]:
        """Map the recorded uid and gid independently, preserving other ids."""

        if self.recorded is None:
            return uid, gid
        return (
            self.host.uid if uid == self.recorded.uid else uid,
            self.host.gid if gid == self.recorded.gid else gid,
        )

    def matches(self, uid: int, gid: int) -> bool:
        """Whether either component of this owner is the recorded account's.

        A record equal to the host's ids maps every such path to itself, so
        the question is what the making host's ``gideon`` owned, never what
        the chown argv changed.
        """

        if self.recorded is None:
            return False
        return uid == self.recorded.uid or gid == self.recorded.gid

    @property
    def is_identity(self) -> bool:
        """Whether this map has no recorded account ids."""

        return self.recorded is None


@dataclass(frozen=True, slots=True)
class PushRecord:
    """The coverage record written at the root of each pushed snapshot."""

    label: str
    pushed_at: datetime
    newest_set: str
    archive_through: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "pushed_at", _utc(self.pushed_at, "pushed_at"))
        object.__setattr__(
            self,
            "archive_through",
            _utc(self.archive_through, "archive_through"),
        )

    def to_json(self) -> str:
        document = {
            "archive_through": _datetime_json(self.archive_through),
            "label": self.label,
            "newest_set": self.newest_set,
            "pushed_at": _datetime_json(self.pushed_at),
        }
        return json.dumps(document, indent=1, sort_keys=True) + "\n"


def parse_push_record(text: str) -> PushRecord | Problem:
    """Parse a push record, returning a field-specific problem on failure."""

    try:
        document = json.loads(text)
        if not isinstance(document, Mapping):
            return _field_problem("push record", "must be an object", _PUSH_RECORD_FIX)
        label = _string_field(document, "label", fix=_PUSH_RECORD_FIX)
        if validate_label(label) is not None:
            return _field_problem("label", "must use the backup label grammar", _PUSH_RECORD_FIX)
        pushed_at = _datetime_field(document, "pushed_at", fix=_PUSH_RECORD_FIX)
        newest_set = _string_field(document, "newest_set", fix=_PUSH_RECORD_FIX)
        archive_through = _datetime_field(
            document, "archive_through", fix=_PUSH_RECORD_FIX
        )
        return PushRecord(label, pushed_at, newest_set, archive_through)
    except json.JSONDecodeError as exc:
        return Problem(f"Push record JSON is malformed: {exc.msg}.", _PUSH_RECORD_FIX)
    except _ParseError as exc:
        return exc.problem
    except (TypeError, ValueError) as exc:
        return Problem(f"Push record is invalid: {exc}.", _PUSH_RECORD_FIX)


@dataclass(frozen=True, slots=True)
class SetRef:
    """A discovered local set, complete only when its manifest parses."""

    label: str
    path: str
    finished: datetime | None
    complete: bool
    manifest: Manifest | None = None

    def __post_init__(self) -> None:
        if self.finished is not None:
            object.__setattr__(self, "finished", _utc(self.finished, "finished"))


def _staging_sets_dir(staging: PathLike) -> str:
    return os.path.join(os.fspath(staging), "sets")


def list_sets(host: Host, staging: PathLike = STAGING) -> tuple[SetRef, ...]:
    """Discover local sets through the injectable host seam."""

    sets_path = _staging_sets_dir(staging)
    try:
        names = host.listdir(sets_path)
    except FileNotFoundError:
        return ()
    refs: list[SetRef] = []
    for name in names:
        path = os.path.join(sets_path, name)
        manifest: Manifest | None = None
        if not name.endswith(PARTIAL_SUFFIX):
            try:
                text = host.read_text(os.path.join(path, MANIFEST_NAME))
            except (OSError, UnicodeError):
                text = ""
            parsed = parse_manifest(text)
            if isinstance(parsed, Manifest):
                manifest = parsed
        if manifest is None:
            refs.append(SetRef(name, path, None, False, None))
        else:
            refs.append(SetRef(name, path, manifest.finished, True, manifest))
    complete = sorted(
        (ref for ref in refs if ref.complete),
        key=lambda ref: (ref.finished or datetime.min.replace(tzinfo=UTC), ref.label),
        reverse=True,
    )
    incomplete = sorted((ref for ref in refs if not ref.complete), key=lambda ref: ref.label)
    return tuple(complete + incomplete)


@dataclass(frozen=True, slots=True)
class RemoteSnapshot:
    """A remote snapshot and its optional push coverage record."""

    label: str
    record: PushRecord | None
    complete: bool


def select_set(
    sets: Sequence[SetRef], at: datetime | None = None
) -> SetRef | Problem:
    """Select the newest complete set, optionally bounded by ``at``."""

    complete = sorted(
        (ref for ref in sets if ref.complete and ref.finished is not None),
        key=lambda ref: cast(datetime, ref.finished),
        reverse=True,
    )
    if not complete:
        return Problem(
            "No complete backup set is available.",
            "Run sudo python3 -m gideon backup run, then retry.",
        )
    if at is None:
        return complete[0]
    try:
        bound = _aware(at, "at")
    except (TypeError, ValueError) as exc:
        return Problem(f"Cannot select a backup set: {exc}.", "Use a timezone-aware --at value.")
    eligible = [ref for ref in complete if cast(datetime, ref.finished) <= bound]
    if eligible:
        return eligible[0]
    oldest = complete[-1]
    assert oldest.finished is not None
    return Problem(
        "No complete backup set finished by --at; the oldest complete set finished "
        f"at {_datetime_json(oldest.finished)}.",
        "Choose a later --at, or omit it for the newest set.",
    )


def select_set_by_label(sets: Sequence[SetRef], label: str) -> SetRef | Problem:
    """Select the complete staging set named by *label*."""

    complete = tuple(ref for ref in sets if ref.complete)
    selected = next((ref for ref in complete if ref.label == label), None)
    if selected is not None:
        return selected
    labels = ", ".join(sorted(ref.label for ref in complete)) or "none"
    return Problem(
        f"No complete backup set is named {label}; the complete sets are: {labels}.",
        "Choose one of them, then retry sudo python3 -m gideon restore --from staging --set <label>.",
    )


_AT_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?$"
)


_AT_FIX: Final = "Provide a valid --at timestamp, then retry restore."


def parse_at(text: str, timezone_name: str) -> datetime | Problem:
    """Parse an ISO 8601 restore bound, using office time for naive input."""

    if not isinstance(text, str) or _AT_PATTERN.fullmatch(text) is None:
        return Problem(
            "The --at value must be ISO 8601 with T or space, optional seconds, "
            "and an optional offset or Z.",
            _AT_FIX,
        )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        return Problem(f"The --at value is not a valid ISO 8601 datetime: {exc}.", _AT_FIX)
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed
    try:
        office_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return Problem(
            f"Office timezone '{timezone_name}' is not available.",
            "Correct office.timezone in /etc/gideon/site.yaml, then retry restore.",
        )
    return parsed.replace(tzinfo=office_zone)


def pgbackrest_target(value: datetime) -> str:
    """Render an aware time in pgBackRest's explicit-offset target form.

    Microseconds stay when present: pgBackRest's time target takes up to six
    fractional digits and an offset, and a set's archive boundary is sub-second,
    so a whole-second target would stop short of what the set describes.
    """

    _aware(value, "target")
    return value.isoformat(sep=" ", timespec="auto")


def prune_candidates(
    sets: Sequence[SetRef],
    partial_mtimes: Mapping[str, float],
    now: datetime,
    local_days: int,
    staging: PathLike = STAGING,
) -> tuple[str, ...]:
    """Return old complete-set and in-flight directory paths for removal."""

    current = _aware(now, "now")
    complete_cutoff = current - timedelta(days=local_days)
    partial_cutoff = current.timestamp() - timedelta(days=1).total_seconds()
    candidates = [
        ref.path
        for ref in sets
        if ref.complete
        and ref.finished is not None
        and ref.finished < complete_cutoff
    ]
    sets_path = _staging_sets_dir(staging)
    candidates.extend(
        os.path.join(sets_path, name)
        for name, mtime in sorted(partial_mtimes.items())
        if name.endswith(PARTIAL_SUFFIX) and mtime < partial_cutoff
    )
    return tuple(candidates)


# The repository's only mutable files: pgBackRest rewrites them on every backup,
# so a set's recorded hash for them holds only until the next run. They are
# never carried forward and never byte-checked against an older manifest;
# pgBackRest's own verify is what proves them.
MUTABLE_REPOSITORY_FILE: Final = re.compile(r"^(?:backup|archive)\.info(?:\.copy)?$")


def carry_forward(previous_entries: Mapping[str, Entry], current: Entry) -> Entry:
    """Reuse a repository hash only when metadata proves the file is unchanged."""

    if current.kind != "f" or MUTABLE_REPOSITORY_FILE.fullmatch(
        current.path.rsplit("/", 1)[-1]
    ):
        return current
    previous = previous_entries.get(current.path)
    if (
        previous is None
        or previous.kind != "f"
        or previous.sha256 is None
        or previous.size != current.size
        or previous.mtime != current.mtime
    ):
        return current
    return replace(current, sha256=previous.sha256)


def sample_paths(
    paths: Sequence[str], percent: int = 1, floor: int = 1
) -> tuple[str, ...]:
    """Select a deterministic percentage sample with a minimum floor."""

    if isinstance(percent, bool) or percent <= 0:
        raise ValueError("percent must be positive")
    if isinstance(floor, bool) or floor < 0:
        raise ValueError("floor must be non-negative")
    ordered = sorted(paths)
    if len(ordered) < floor:
        return tuple(ordered)
    step = max(1, 100 // percent)
    selected = set(range(0, len(ordered), step))
    selected.update(range(min(floor, len(ordered))))
    return tuple(ordered[index] for index in sorted(selected))
