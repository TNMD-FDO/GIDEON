"""Restore orchestration contracts over a dict-backed host seam."""

import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from derivedstores_fake import (
    empty_store_secrets,
    level_store_result,
    recovery_store_result,
)
from test_index_level import generation

from gideon.host import (
    apply,
    backuplock,
    backuproots,
    backupset,
    cas,
    nogpu,
    report,
    restore,
    secrets,
    site,
    sshtarget,
    stack,
    stores,
    worker,
)
from gideon.host.corpus.record import database_fix
from gideon.host.index import level
from gideon.host.index.record import DemotionOutcome
from gideon.host.render import RenderedSet, RenderInputs
from gideon.host.render.command import RenderOutcome
from gideon.host.render.compose import STORE_SERVICES
from gideon.host.steps.site_dirs import AGE_RECIPIENT_PATH
from gideon.host.sysio import Command, Host, PathLike

RENDERED = "/etc/gideon/rendered"
SITE_PATH = "/etc/gideon/site.yaml"
LOCAL_SET = "20260902T110000Z"
LATER_SET = "20260902T115000Z"
REMOTE_SNAPSHOT = "20260902T120000Z"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
RECIPIENT = "age1" + "a" * 58
SECOND_RECIPIENT = "age1" + "c" * 58
DEFAULT_GIDEON_IDS = backupset.AccountIds(999, 983)
TARBALL_BYTES = b"tarball"
TARBALL_SHA256 = hashlib.sha256(TARBALL_BYTES).hexdigest()
SELECT_FIX = "Choose an earlier --at, or omit it for the latest state."
SECRET_DIGESTS = {"webui_secret_key": "1" * 64, "ldap_bind_password": "2" * 64}
_fingerprint = secrets.secrets_fingerprint(SECRET_DIGESTS)
assert _fingerprint is not None
FINGERPRINT: str = _fingerprint
STORE_ROOT = next(root for root in backupset.inventory_roots("/work/GIDEON") if root.kind is backupset.RootKind.STORE)
UPLOADS_ROOT = next(root for root in backupset.inventory_roots("/work/GIDEON") if root.user_written)
DERIVED_ROOTS = tuple(
    root for root in backupset.inventory_roots("/work/GIDEON") if root.set_may_predate
)
REGISTRY_ROOT = next(root for root in backupset.inventory_roots("/work/GIDEON") if root.name == "data-registry")
POSTGRES_IMAGE = "127.0.0.1:5000/postgres@sha256:" + "d" * 64
POSTGRES_COMPOSE = f"services:\n  postgres:\n    image: {POSTGRES_IMAGE}\n"

SITE = """\
office:
  name: Test Office
  short_name: TEST
  timezone: Etc/UTC
hostname: gideon.test
lan_cidrs: [192.0.2.0/24]
jurisdiction:
  circuit: ca6
  districts: [tnmd]
  states: [TN]
auth:
  ldap:
    host: ad.test
backup:
  target:
    host: nas.test
    path: /backup/snapshots
alerts:
  smtp:
    host: smtp.test
    from: gideon@test
  recipients: [csa@test]
"""


def completed(
    argv: Sequence[str], stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def entry(
    path: str,
    *,
    digest: str = "a" * 64,
    uid: int = 1000,
    gid: int = 1000,
    mode: int = 0o644,
) -> backupset.Entry:
    return backupset.Entry(path, "f", 7, uid, gid, mode, 100.0, digest)


def manifest(*, finished: datetime = NOW - timedelta(hours=1)) -> backupset.Manifest:
    return backupset.Manifest(
        1,
        LOCAL_SET,
        backupset.Kind.NIGHTLY,
        finished - timedelta(minutes=10),
        finished,
        "0.0.18",
        "/work/GIDEON",
        "commit",
        "gideon.test",
        None,
        "backup-label",
        "full",
        finished,
        {"gideon": {"public.users": 2}, "openwebui": {}},
        {
            "etc-gideon": (entry("config.yaml"),),
            "checkout": (entry("gideon/__init__.py"),),
            "data-registry": (entry("image.blob"),),
            "data-bulk-openwebui": (entry("uploads/data.bin"),),
            "pgbackrest": (
                entry("backup/gideon/backup.info", mode=0o640),
                entry("archive/gideon/archive.info", digest="b" * 64, mode=0o640),
                entry(
                    "backup/gideon/backup-label/backup.manifest",
                    digest="c" * 64,
                    mode=0o640,
                ),
            ),
        },
        TARBALL_SHA256,
        (RECIPIENT, SECOND_RECIPIENT),
        backupset.LinkVerdict(1, 1),
        FINGERPRINT,
    )


def manifest_with_store(sizes: Mapping[str, int]) -> tuple[backupset.Manifest, str]:
    """Pin a fictitious listing built from the objects named by a case."""

    listing = "".join(f"{name}\t{size}\n" for name, size in sorted(sizes.items()))
    pin = backupset.ListingPin(
        f"{STORE_ROOT.name}.listing", len(listing.encode()), hashlib.sha256(listing.encode()).hexdigest(),
        len(sizes), sum(sizes.values()),
    )
    return replace(manifest(), listings={STORE_ROOT.name: pin}), listing


def manifest_with_snapshots(*roots: backupset.InventoryRoot) -> backupset.Manifest:
    """A set that holds the requested derived roots, including empty copies."""

    value = manifest()
    return replace(value, inventory={**value.inventory, **{root.name: () for root in roots}})


@dataclass(frozen=True)
class Replay:
    """A fictitious recovery's last commit and WAL position."""

    instant: datetime | None = NOW - timedelta(seconds=10)
    lsn: str | None = "0/A000000"
    segment: str = "00000003000000000000000A"
    segment_size: int = 16 * 1024**2
    recovery_polls: int = 0


class FakeHost:
    """A recording Host with independent local and fetched set listings."""

    def __init__(
        self,
        *,
        running: bool = False,
        target: bool = True,
        source: str = "staging",
        names: Sequence[str] = (LOCAL_SET,),
        side_names: Sequence[str] = (LOCAL_SET,),
        remote_listing: str = "",
        remote_record: backupset.PushRecord | None = None,
        remote_records: Mapping[str, backupset.PushRecord] | None = None,
        fail_checksum: bool = False,
        fail_pgbackrest: bool = False,
        info_has_label: bool = True,
        archive_max: str | None = "00000002000000000000000A",
        history_files: Mapping[str, str] | None = None,
        lsn_stop: str | None = "0/7000000",
        backup_timeline: int = 2,
        replay: Replay | None = None,
        fail_identity_exec: bool = False,
        secrets_present: bool = False,
        partial: bool = False,
        sets_absent: bool = False,
        fail_swap: bool = False,
        manifest_value: backupset.Manifest | None = None,
        physical_escape: bool = False,
        extra_physical_file: tuple[str, str] | None = None,
        registry_active: bool = False,
        registry_has_image: bool = True,
        registry_image_after_restart: bool = True,
        gideon_ids: backupset.AccountIds = DEFAULT_GIDEON_IDS,
        fail_gideon_ids: bool = False,
        store_listing: str | None = None,
        store_live: Mapping[str, int] | None = None,
        store_damaged: Sequence[str] = (),
        qdrant_files: Sequence[str] = (),
        opensearch_snapshots: Sequence[Mapping[str, object]] = (),
        qdrant_status: int = 200,
        restore_failed: int = 0,
        generations: Sequence[Mapping[str, Any]] = (),
        index_counts: Mapping[str, Mapping[str, int | None]] | None = None,
        index_aliases: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
        generation_read_code: int = 0,
        generation_demotion_code: int = 0,
        level_store_code: int = 0,
    ) -> None:
        value = manifest_value or manifest()
        self.manifest_value = value
        self.store_copy = backupset.store_copy(backupset.STAGING, backupset.set_dir(LOCAL_SET), value, STORE_ROOT.name)
        self.partial = partial
        self.fail_swap = fail_swap
        self.physical_escape = physical_escape
        self.extra_physical_file = extra_physical_file
        self.registry_active = registry_active
        self.registry_has_image = registry_has_image
        self.registry_image_after_restart = registry_image_after_restart
        self.gideon_ids = gideon_ids
        self.fail_gideon_ids = fail_gideon_ids
        self.store_live = None if store_live is None else dict(store_live)
        self.store_damaged = set(store_damaged)
        self.qdrant_files = tuple(qdrant_files)
        self.opensearch_snapshots = tuple(opensearch_snapshots)
        self.qdrant_status = qdrant_status
        self.restore_failed = restore_failed
        self.generations = [dict(row) for row in generations]
        self.index_counts: dict[str, dict[str, int | None]] = {
            "opensearch": {
                row["gen_id"]: row["counts"].get("indexed", 0) + row["counts"].get("present", 0)
                for row in generations
            },
            "qdrant": {row["gen_id"]: row["counts"].get("dense_total", 0) for row in generations},
        } if index_counts is None else {store: dict(counts) for store, counts in index_counts.items()}
        self.index_aliases = {store: dict(targets) for store, targets in (index_aliases or {}).items()}
        self.generation_statements: list[str] = []
        self.generation_read_code = generation_read_code
        self.generation_demotion_code = generation_demotion_code
        self.level_store_code = level_store_code
        self.running = running
        self.target = target
        self.source = source
        self.names = list(names)
        self.side_names = list(side_names)
        self.sets_absent = sets_absent
        self.remote_listing = remote_listing
        self.remote_record = remote_record or backupset.PushRecord(
            REMOTE_SNAPSHOT, NOW, LOCAL_SET, value.archive_through
        )
        self.remote_records = dict(remote_records or {})
        self.fail_checksum = fail_checksum
        self.fail_pgbackrest = fail_pgbackrest
        self.info_has_label = info_has_label
        self.archive_max = archive_max
        self.history_files = dict(history_files) if history_files is not None else {
            "00000002.history": "1\t0/4000000\tfictitious fork\n",
        }
        self.lsn_stop = lsn_stop
        self.backup_timeline = backup_timeline
        self.replay = replay or Replay()
        self.recovery_polls_remaining = self.replay.recovery_polls
        self.fail_identity_exec = fail_identity_exec
        self.calls: list[tuple[tuple[str, ...], str | None, str | None, float | None]] = []
        self.events: list[str] = []
        self.render_calls: list[dict[str, object]] = []
        self.render_outcome: RenderOutcome | None = None
        self.render_error: Exception | None = None
        self.files: dict[str, str] = {
            **empty_store_secrets(),
            SITE_PATH: SITE,
            os.fspath(AGE_RECIPIENT_PATH): RECIPIENT + "\n",
            os.path.join(RENDERED, "compose.yaml"): "services: {}\n",
            "/usr/bin/bash": "",
            "/usr/bin/age": "",
            "/usr/bin/rsync": "",
            os.path.join(
                backupset.set_dir(LOCAL_SET), backupset.MANIFEST_NAME
            ): value.to_json(),
        }
        if store_listing is not None:
            self.files[backupset.listing_path(backupset.set_dir(LOCAL_SET), STORE_ROOT.name)] = store_listing
        self.secrets_present = secrets_present
        self.locks: dict[str, str] = {}

    def _local_path(self, path: str) -> str:
        """Use the local fixture for a fetched set's identical listing."""

        side = f"{backupset.STAGING}.fetch-{REMOTE_SNAPSHOT}"
        if path.startswith(side + "/sets/"):
            return backupset.STAGING + path.removeprefix(side)
        return path

    def _root_for(self, base: str) -> str:
        """Which inventory root a find base names: a set's files/<root>, a live source, or the repository."""

        if "/files/" in base:
            return base.rsplit("/files/", 1)[1].split("/", 1)[0]
        for root in backupset.inventory_roots(self.manifest_value.checkout):
            if base == root.source:
                return root.name
        if base.endswith("/pgbackrest"):
            return "pgbackrest"
        return ""

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, env
        command = tuple(argv)
        self.calls.append((command, input, None if cwd is None else os.fspath(cwd), timeout))
        if "psql" in command and input is not None:
            if "pg_is_in_recovery()" in input:
                recovering = self.recovery_polls_remaining > 0
                self.recovery_polls_remaining -= int(recovering)
                return completed(command, stdout="t\n" if recovering else "f\n")
            if "pg_last_xact_replay_timestamp()" in input:
                if self.recovery_polls_remaining:
                    return completed(command, returncode=1, stderr="still in recovery")
                reach = self.replay
                return completed(command, stdout="\t".join((
                    reach.instant.isoformat() if reach.instant is not None else "",
                    reach.lsn or "", reach.segment, str(reach.segment_size),
                )) + "\n")
        if command == tuple(worker.psql_argv(RENDERED)):
            assert input is not None
            self.generation_statements.append(input)
            if "SELECT row_to_json(generation)" in input:
                return completed(
                    command, stdout="".join(json.dumps(row) + "\n" for row in self.generations),
                    returncode=self.generation_read_code,
                )
            assert "DO $demotion$" in input
            if self.generation_demotion_code != 0:
                return completed(command, returncode=self.generation_demotion_code)
            payload = json.loads(input.split("\n", 1)[0].removeprefix("\\set v_payload '").removesuffix("'"))
            row = next((row for row in self.generations if row["gen_id"] == payload["name"]), None)
            if row is None or row["state"] not in ("built", "live"):
                return completed(command, returncode=3, stderr="generation is no longer built or live")
            outcome = DemotionOutcome(
                row["gen_id"], row["state"], payload["lexical_short"], payload["dense_short"],
            )
            row.update(state="building", built_at=None, promoted_at=None)
            if payload["lexical_short"]:
                row["lexical_built_at"] = None
            if payload["dense_short"]:
                row["dense_built_at"] = None
            return completed(command, stdout=json.dumps(asdict(outcome)) + "\n")
        if command[:3] == ("docker", "manifest", "inspect"):
            self.events.append("registry-probe")
            return completed(command, returncode=0 if self.registry_has_image else 1)
        if command == ("systemctl", "restart", "gideon-registry"):
            self.events.append("registry-restart")
            self.registry_has_image = self.registry_image_after_restart
            return completed(command)
        if command == ("systemctl", "start", "gideon-registry"):
            self.events.append("registry-start")
        if command == tuple(stack.compose_argv(RENDERED, "up", "-d", *STORE_SERVICES)):
            self.events.append("store-up")
        if command == ("getent", "passwd", backupset.SERVICE_ACCOUNT):
            if self.fail_gideon_ids:
                return completed(command, returncode=1)
            return completed(
                command,
                stdout=(
                    f"{backupset.SERVICE_ACCOUNT}:x:{self.gideon_ids.uid}:"
                    f"{self.gideon_ids.gid}::/home/{backupset.SERVICE_ACCOUNT}:/bin/bash\n"
                ),
            )
        if command == ("systemctl", "is-active", "gideon-registry"):
            return completed(command, returncode=0 if self.registry_active else 3)
        if "exec" in command and "pg_isready" in command:
            return completed(command, returncode=0 if self.running and not self.partial else 1)
        if command[:2] == ("mv", f"{backupset.STAGING}.fetch-{REMOTE_SNAPSHOT}") and self.fail_swap:
            return completed(command, returncode=1)
        if command[:2] == ("docker", "compose") and "up" in command:
            self.running = True
            self.partial = False
        if command[:2] == ("docker", "compose") and command[-1] == "down":
            self.running = False
        if "exec" in command and "id" in command:
            if self.fail_identity_exec:
                return completed(command, returncode=1)
            if command[-2:] == ("-u", "postgres"):
                return completed(command, stdout="1000\n")
            if command[-2:] == ("-g", "postgres"):
                return completed(command, stdout="1001\n")
        if command and command[0] == "ssh":
            if command[-1] == "true":
                return completed(command, returncode=int(not self.target))
            if "for directory in */" in command[-1]:
                return completed(command, stdout=self.remote_listing)
            if len(command) >= 2 and command[-2] == "cat":
                record = self.remote_record
                for name, candidate in self.remote_records.items():
                    if f"/{name}/push.json" in command[-1]:
                        record = candidate
                        break
                return completed(command, stdout=record.to_json())
        if command and command[0] == "rsync":
            if "--files-from=-" in command:
                paths = (input or "").splitlines()
                set_path = (
                    backupset.set_dir(LOCAL_SET)
                    if self._local_path(command[-2].rstrip("/")) == self.store_copy
                    else command[-2].rstrip("/").rsplit("/files/", 1)[0]
                )
                listing_path = self._local_path(backupset.listing_path(set_path, STORE_ROOT.name))
                sizes = dict(line.split("\t", 1) for line in self.files[listing_path].splitlines())
                if self.store_live is not None:
                    for path in paths:
                        name = path.rsplit("/", 1)[-1]
                        self.store_live[name] = int(sizes[name])
                count = len(paths)
                return completed(command, stdout=f"Number of regular files transferred: {count}\n")
            if command[:3] == ("rsync", "-a", "--stats"):
                count = int(f"/sets/{LATER_SET}/files/{UPLOADS_ROOT.name}/" in command[-2])
                return completed(command, stdout=f"Number of regular files transferred: {count}\n")
            return completed(command)
        if command[:2] == ("find", ".") and "-fprintf" in command:
            listing_path = command[command.index("-fprintf") + 1]
            self.files[listing_path] = "".join(
                f"{name}\t{size}\n" for name, size in sorted((self.store_live or {}).items())
            )
            return completed(command)
        if command[:3] == ("env", "LC_ALL=C", "sort") and "-o" in command:
            path = command[-1]
            self.files[path] = "".join(sorted(self.files[path].splitlines(keepends=True)))
            return completed(command)
        if command[:3] == ("env", "LC_ALL=C", "join"):
            set_lines = self.files[self._local_path(command[-2])].splitlines()
            live_names = {line.partition("\t")[0] for line in self.files[command[-1]].splitlines()}
            return completed(
                command,
                stdout="".join(f"{line}\n" for line in set_lines if line.partition("\t")[0] not in live_names),
            )
        if command[0] == "find" and command[-2:] == ("-printf", backupset.FIND_FORMAT):
            # The physical tree under one base: that root's inventoried entries
            # with their declared kinds, unless the tree has been made to escape.
            base = str(command[1])
            root_name = self._root_for(base)
            value = self.manifest_value
            if "/files/" in base:
                set_path = base.split("/files/", 1)[0]
                document = self.files[self._local_path(os.path.join(set_path, backupset.MANIFEST_NAME))]
                parsed = backupset.parse_manifest(document)
                assert isinstance(parsed, backupset.Manifest)
                value = parsed
            records = "d\t4096\t0\t0\t755\t100.0\t\0"
            for item in value.inventory.get(root_name, ()):
                if self.physical_escape and item.path.startswith("escape/"):
                    continue
                records += f"{item.kind}\t{item.size}\t{item.uid}\t{item.gid}\t{item.mode:o}\t{item.mtime}\t{item.path}\0"
            if self.physical_escape:
                records += "l\t4\t0\t0\t777\t100.0\tescape\0"
            if self.extra_physical_file is not None and self.extra_physical_file[0] == root_name:
                records += f"f\t3\t0\t0\t644\t100.0\t{self.extra_physical_file[1]}\0"
            return completed(command, stdout=records)
        if command[:2] == ("find", "/etc/gideon/secrets"):
            digests = SECRET_DIGESTS if self.secrets_present else {"webui_secret_key": "9" * 64}
            return completed(
                command,
                stdout="".join(f"{digest}  /etc/gideon/secrets/{name}\n" for name, digest in digests.items()),
            )
        if command[:2] == ("sha256sum", "-c"):
            if not (input or ""):
                # The real tool refuses an empty list.
                return completed(command, returncode=1, stderr="sha256sum: 'standard input': no properly formatted checksum lines found\n")
            if self.fail_checksum:
                return completed(command, returncode=1, stdout="config.yaml: FAILED\n")
            if cwd is not None and f"/sets/{LATER_SET}/files/{UPLOADS_ROOT.name}" in os.fspath(cwd):
                base = self._local_path(os.fspath(cwd))
                checks = []
                for line in (input or "").splitlines():
                    digest, path = line.split("  ", 1)
                    content = self.files.get(os.path.join(base, path))
                    okay = content is not None and hashlib.sha256(content.encode()).hexdigest() == digest
                    checks.append((path, okay))
                return completed(
                    command,
                    stdout="".join(f"{path}: {'OK' if okay else 'FAILED'}\n" for path, okay in checks),
                    returncode=int(any(not okay for _, okay in checks)),
                )
            if cwd is not None and (
                self._local_path(os.fspath(cwd)) == self.store_copy
                or os.fspath(cwd).endswith("/files/" + STORE_ROOT.name)
            ):
                lines = (input or "").splitlines()
                damaged_here = self.store_damaged if self._local_path(os.fspath(cwd)) == self.store_copy else set()
                output = "".join(
                    f"{line.split('  ', 1)[1]}: {'FAILED' if line.split('  ', 1)[0] in damaged_here else 'OK'}\n"
                    for line in lines
                )
                return completed(command, stdout=output, returncode=int(any(line.split("  ", 1)[0] in damaged_here for line in lines)))
            output = "".join(
                line.split("  ", 1)[1].rstrip("\n") + ": OK\n"
                for line in (input or "").splitlines(True)
            )
            return completed(command, stdout=output)
        if command and command[0] == "sha256sum":
            path = self._local_path(command[-1])
            if path in self.files and path.endswith("/" + STORE_ROOT.name + ".listing"):
                return completed(command, stdout=f"{hashlib.sha256(self.files[path].encode()).hexdigest()}  {command[-1]}\n")
            return completed(command, stdout=f"{TARBALL_SHA256}  {command[-1]}\n")
        if command and command[0] == "pgbackrest":
            return completed(command, returncode=int(self.fail_pgbackrest))
        if "pgbackrest" in command and "verify" in command:
            return completed(command, returncode=int(self.fail_pgbackrest))
        if "pgbackrest" in command and "info" in command and "--output=json" in command:
            labels = [self.manifest_value.pgbackrest_label] if self.info_has_label else []
            backups = [{
                "label": label, "type": "full", "timestamp": {"stop": 100},
                "archive": {"start": f"{self.backup_timeline:08X}0000000000000005"},
                "lsn": {"stop": self.lsn_stop},
            } for label in labels]
            return completed(command, stdout=json.dumps([{
                "name": "gideon", "backup": backups,
                "archive": [{"database": {"id": 1}, "id": "1000-1", "max": self.archive_max}],
            }]))
        if "pgbackrest" in command and "repo-ls" in command and "--output=json" in command:
            return completed(command, stdout=json.dumps({
                name: {"type": "file"} for name in self.history_files
            }))
        if "pgbackrest" in command and "repo-get" in command:
            name = command[-1].rsplit("/", 1)[-1]
            if name not in self.history_files:
                return completed(command, returncode=1, stderr="fictitious history file missing")
            return completed(command, stdout=self.history_files[name])
        if "run" in command and "id" in command:
            flag = "-u" if "-u" in command else "-g"
            return completed(command, stdout=("1000" if flag == "-u" else "1001") + "\n")
        if len(command) >= 2 and command[-2:] == ("--format", "json"):
            if self.partial:
                return completed(command, stdout='[{"Service":"open-webui","State":"running","Health":"healthy"}]')
            if not self.running:
                return completed(command, stdout="[]")
            return completed(
                command,
                stdout=(
                    '[{"Service":"postgres","State":"running","Health":"healthy"},'
                    '{"Service":"qdrant","State":"running","Health":"healthy"},'
                    '{"Service":"opensearch","State":"running","Health":"healthy"}]'
                ),
            )
        level_reply = level_store_result(
            command, input, RENDERED, counts=self.index_counts, aliases=self.index_aliases,
        )
        if level_reply is not None:
            if self.level_store_code != 0:
                return completed(command, returncode=self.level_store_code)
            return level_reply
        recovery_reply = recovery_store_result(
            command, input, RENDERED,
            qdrant_files=self.qdrant_files,
            opensearch_snapshots=self.opensearch_snapshots,
            qdrant_status=self.qdrant_status,
            restore_failed=self.restore_failed,
        )
        return recovery_reply if recovery_reply is not None else completed(command)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = self._local_path(os.fspath(path))
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding, mode
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files or (os.fspath(path) == os.fspath(cas.ROOT) and self.store_live is not None)

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path)
        if key == backupset.SETS_DIR:
            if self.sets_absent:
                raise FileNotFoundError(key)
            return list(self.names)
        if key == f"{backupset.STAGING}.fetch-{REMOTE_SNAPSHOT}/sets":
            return list(self.side_names)
        if key.endswith("/sets") and ".fetch-" in key:
            return list(self.side_names)
        raise FileNotFoundError(key)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        key = self._local_path(os.fspath(path))
        if key in self.files and key.endswith("/" + STORE_ROOT.name + ".listing"):
            return os.stat_result((0o100644, 1, 0, 1, 0, 0, len(self.files[key].encode()), 0, 0, 0))
        if os.fspath(path) == "/data/registry":
            return os.stat_result((0o40755, 1, 0, 1, 999, 983, 0, 0, 0, 0))
        raise FileNotFoundError(os.fspath(path))

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = record
        return None

    def release_lock(self, path: PathLike) -> None:
        self.locks.pop(os.fspath(path), None)

    def geteuid(self) -> int:
        return 0


def make_host(**kwargs: Any) -> FakeHost:
    return FakeHost(**kwargs)


class RenderedRestoreCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()

        def render(io: Host, **kwargs: object) -> RenderOutcome:
            assert isinstance(io, FakeHost)
            io.events.append("render")
            io.render_calls.append(kwargs)
            if io.render_error is not None:
                raise io.render_error
            return io.render_outcome or RenderOutcome(
                rendered=cast(RenderedSet, object()),
                inputs=cast(RenderInputs, object()),
                exit_code=0,
            )

        patcher = patch.object(restore, "render_to_disk", side_effect=render)
        patcher.start()
        self.addCleanup(patcher.stop)


class RegistrySeed(RenderedRestoreCase):
    def _host(self, **kwargs: Any) -> FakeHost:
        fake = make_host(
            source="target", remote_listing=f"{REMOTE_SNAPSHOT}\t1\n", **kwargs
        )
        fake.files[os.fspath(nogpu.BUILD_BOX_PATH)] = "declared\n"
        fake.files[SITE_PATH] += "\nregistry: 127.0.0.1:5000\n"
        fake.files[os.path.join(RENDERED, "compose.yaml")] = POSTGRES_COMPOSE
        return fake

    def _run(
        self, fake: FakeHost, *, at: datetime | None = None
    ) -> tuple[int, str, list[float]]:
        output = io.StringIO()
        errors = io.StringIO()
        sleeps: list[float] = []
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=at.isoformat() if at is not None else None),
                host=fake, now=NOW, sleep=sleeps.append,
            )
        self.assertEqual(errors.getvalue(), "")
        return code, output.getvalue(), sleeps

    def _registry_copy(self, label: str) -> str:
        side = f"{backupset.STAGING}.fetch-{backupset.nightly_label(NOW)}"
        return os.path.join(backupset.sets_dir(side), label, backupset.FILES_DIR, REGISTRY_ROOT.name)

    def test_absent_image_is_verified_seeded_and_probed_before_identity_lookup(self) -> None:
        fake = self._host(registry_has_image=False, fail_identity_exec=True)
        code, output, sleeps = self._run(fake)

        self.assertEqual(code, 0, output)
        self.assertIn("verify: ok", output)
        self.assertIn(
            f"fetched {REMOTE_SNAPSHOT}; selected set {LOCAL_SET}; "
            f"seeded the host registry from set {LOCAL_SET}'s {REGISTRY_ROOT.name} copy "
            "and restarted it; re-owned ",
            output,
        )
        copy = self._registry_copy(LOCAL_SET)
        probes = [index for index, call in enumerate(fake.calls) if call[0][:3] == ("docker", "manifest", "inspect")]
        self.assertEqual(len(probes), 2)
        for index in probes:
            self.assertEqual(fake.calls[index][0], ("docker", "manifest", "inspect", "--insecure", POSTGRES_IMAGE))
        checked = next(index for index, call in enumerate(fake.calls) if call[0] == ("sha256sum", "-c", "-") and call[2] == copy)
        expected_checks = "".join(
            f"{item.sha256}  {item.path}\n" for item in fake.manifest_value.inventory[REGISTRY_ROOT.name]
            if item.kind == "f"
        )
        self.assertEqual(fake.calls[checked][1], expected_checks)
        seeded = next(index for index, call in enumerate(fake.calls) if call[0] == ("rsync", "-a", copy + "/", REGISTRY_ROOT.source + "/"))
        self.assertNotIn("--delete", fake.calls[seeded][0])
        restarted = next(index for index, call in enumerate(fake.calls) if call[0] == ("systemctl", "restart", "gideon-registry"))
        identity = next(index for index, call in enumerate(fake.calls) if "id" in call[0])
        one_off = next(index for index, call in enumerate(fake.calls) if "id" in call[0] and "run" in call[0])
        order = (probes[0], checked, seeded, restarted, probes[1], identity, one_off)
        for before, after in pairwise(order):
            self.assertLess(before, after)
        self.assertEqual(fake.events[:3], ["registry-probe", "registry-restart", "registry-probe"])
        self.assertEqual(sleeps, [])

    def test_present_image_skips_seed_and_restart(self) -> None:
        fake = self._host()
        code, output, sleeps = self._run(fake)

        self.assertEqual(code, 0, output)
        self.assertEqual(fake.events.count("registry-probe"), 1)
        self.assertNotIn("registry-restart", fake.events)
        stopped = next(index for index, call in enumerate(fake.calls) if call[0][-1] == "down")
        self.assertFalse(any(
            call[0][0] == "rsync" and call[0][-1] == REGISTRY_ROOT.source + "/"
            for call in fake.calls[:stopped]
        ))
        fetch = next(line for line in output.splitlines() if line.startswith("fetch:"))
        self.assertIn("; the host registry holds the Postgres image; re-owned ", fetch)
        self.assertNotIn("seeded", fetch)
        self.assertEqual(sleeps, [])

    def test_other_hosts_neither_probe_nor_name_a_seed_clause(self) -> None:
        fake = make_host(
            source="target", remote_listing=f"{REMOTE_SNAPSHOT}\t1\n", registry_has_image=False
        )
        code, output, sleeps = self._run(fake)

        self.assertEqual(code, 0, output)
        self.assertFalse(any(call[0][:3] == ("docker", "manifest", "inspect") for call in fake.calls))
        self.assertNotIn("registry-restart", fake.events)
        fetch = next(line for line in output.splitlines() if line.startswith("fetch:"))
        self.assertIn(f"selected set {LOCAL_SET}; re-owned ", fetch)
        self.assertNotIn("registry", fetch)
        self.assertEqual(sleeps, [])

    def test_seed_without_the_image_refuses_with_the_sets_release_after_the_bound(self) -> None:
        fake = self._host(registry_has_image=False, registry_image_after_restart=False)
        code, output, sleeps = self._run(fake)

        self.assertEqual(code, 1, output)
        fetch = next(line for line in output.splitlines() if line.startswith("fetch:"))
        detail, fix = fetch.split(" Fix: ", 1)
        release = fake.manifest_value.release
        self.assertEqual(
            detail,
            "fetch: refuse — the host registry lacks the Postgres image after the seed "
            f"from set {LOCAL_SET} (release {release})",
        )
        self.assertIn(f"Check out tag v{release}", fix)
        self.assertIn(report.command("apply"), fix)
        self.assertIn("then retry restore", fix)
        self.assertIn("docs/runbooks/backup-restore.md §5.", fix)
        self.assertEqual(sum(sleeps), restore.REGISTRY_SEED_WAIT_SECONDS)
        self.assertTrue(all(0 < pause <= restore._REGISTRY_SEED_PROBE_INTERVAL_SECONDS for pause in sleeps))
        self.assertEqual(fake.events, ["registry-probe", "registry-restart", *["registry-probe"] * (len(sleeps) + 1)])
        self.assertFalse(any(call[0][-1] == "down" or call[0][:2] == ("systemctl", "stop") for call in fake.calls))
        self.assertFalse(any("id" in call[0] for call in fake.calls))
        self.assertNotIn("verify:", output)

    def test_missing_or_fileless_registry_copy_refuses_with_the_build_fix(self) -> None:
        value = manifest()
        without_registry = {name: entries for name, entries in value.inventory.items() if name != REGISTRY_ROOT.name}
        directory = replace(entry("blobs"), kind="d", sha256=None)
        for inventory in (
            without_registry,
            {**without_registry, REGISTRY_ROOT.name: ()},
            {**without_registry, REGISTRY_ROOT.name: (directory,)},
        ):
            with self.subTest(entries=inventory.get(REGISTRY_ROOT.name)):
                fake = self._host(registry_has_image=False, manifest_value=replace(value, inventory=inventory))
                code, output, sleeps = self._run(fake)

                self.assertEqual(code, 1, output)
                self.assertIn(f"fetch: refuse — set {value.label} carries no registry copy Fix: ", output)
                self.assertIn("Build and push the built images with python3 -m tools.imagebuild <name>", output)
                self.assertIn("docs/runbooks/built-images.md §2, then retry restore.", output)
                self.assertEqual(fake.events, ["registry-probe"])
                self.assertFalse(any(call[0][0] == "rsync" and call[0][-1] == REGISTRY_ROOT.source + "/" for call in fake.calls))
                self.assertFalse(any("id" in call[0] or call[0][-1] == "down" for call in fake.calls))
                self.assertEqual(sleeps, [])

    def test_rendered_postgres_without_an_image_refuses_with_the_apply_fix(self) -> None:
        fake = self._host()
        fake.files[os.path.join(RENDERED, "compose.yaml")] = "services:\n  postgres: {}\n"
        code, output, sleeps = self._run(fake)

        self.assertEqual(code, 1, output)
        self.assertIn(
            "fetch: refuse — rendered Compose file names no image for postgres. "
            f"Fix: Run {report.command('apply')}, then retry.",
            output,
        )
        self.assertEqual(fake.events, [])
        self.assertFalse(any("id" in call[0] or call[0][-1] == "down" for call in fake.calls))
        self.assertEqual(sleeps, [])

    def test_at_selects_the_older_registry_copy_before_identity_lookup(self) -> None:
        at = NOW - timedelta(minutes=30)
        older = replace(manifest(), archive_through=at)
        later_finished = NOW - timedelta(minutes=10)
        newer = replace(
            older, label=LATER_SET, finished=later_finished, archive_through=later_finished,
            inventory={name: entries for name, entries in older.inventory.items() if name != REGISTRY_ROOT.name},
        )
        fake = self._host(
            registry_has_image=False, manifest_value=older, side_names=(LATER_SET, LOCAL_SET),
            remote_record=backupset.PushRecord(REMOTE_SNAPSHOT, NOW, LATER_SET, NOW),
        )
        fake.files[os.path.join(backupset.set_dir(LATER_SET), backupset.MANIFEST_NAME)] = newer.to_json()
        code, output, sleeps = self._run(fake, at=at)

        self.assertEqual(code, 0, output)
        self.assertIn(f"selected set {older.label}; seeded the host registry from set {older.label}'s", output)
        seed = ("rsync", "-a", self._registry_copy(older.label) + "/", REGISTRY_ROOT.source + "/")
        seeded = next(index for index, call in enumerate(fake.calls) if call[0] == seed)
        identity = next(index for index, call in enumerate(fake.calls) if "id" in call[0])
        self.assertLess(seeded, identity)
        self.assertFalse(any(call[0][:2] == ("sha256sum", "-c") and call[2] == self._registry_copy(newer.label) for call in fake.calls))
        self.assertEqual(sleeps, [])

    def test_at_without_an_eligible_set_refuses_before_identity_lookup(self) -> None:
        fake = self._host()
        code, output, sleeps = self._run(fake, at=fake.manifest_value.finished - timedelta(seconds=1))

        self.assertEqual(code, 1, output)
        self.assertIn("fetch: refuse — No complete backup set finished by --at;", output)
        self.assertEqual(fake.events, [])
        self.assertFalse(any("id" in call[0] or call[0][-1] == "down" for call in fake.calls))
        self.assertEqual(sleeps, [])


class RestoreContracts(RenderedRestoreCase):
    def test_refuses_while_a_foreign_holder_has_the_lock(self) -> None:
        fake = make_host()
        holder = backuplock.Record("backup push", os.getpid() + 1, NOW)
        stored = holder.to_json()
        fake.locks[backuplock.BACKUP_LOCK.path] = stored
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(holder.command, err.getvalue())
        self.assertIn(holder.started.isoformat(), err.getvalue())
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.locks[backuplock.BACKUP_LOCK.path], stored)

    def test_staging_restore_verifies_before_down_and_prints_next_steps(self) -> None:
        fake = make_host()
        fake.files[os.fspath(nogpu.BUILD_BOX_PATH)] = "declared\n"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0)
        self.assertEqual(fake.manifest_value.recipients, (RECIPIENT, SECOND_RECIPIENT))
        names = [
            line.split(":", 1)[0]
            for line in out.getvalue().splitlines()
            if line.startswith(("select:", "pre-restore:", "fetch:", "verify:", "stop:", "swap:", "files:", "postgres:", "stores:", "next:"))
        ]
        self.assertEqual(
            names,
            ["select", "pre-restore", "fetch", "verify", "stop", "swap", "files", "postgres", "stores", "next"],
        )
        down_index = next(index for index, call in enumerate(fake.calls) if call[0][-1] == "down")
        verify_index = next(index for index, call in enumerate(fake.calls) if "verify" in call[0])
        self.assertLess(verify_index, down_index)
        self.assertIn("age -d -i <identity file kept off-box>", out.getvalue())
        self.assertIn("not the set's", out.getvalue())
        self.assertIn("Next: sudo python3 -m gideon host provision", out.getvalue())
        self.assertIn("stop: ok — stopped the host registry", out.getvalue())
        self.assertIn(
            "stores: ok — frontend and ingress down; the store tier running; qdrant: no snapshot in the set; opensearch: no snapshot in the set; level: no generation recorded; the host registry running",
            out.getvalue(),
        )
        self.assertIn(
            "next: ok — frontend and ingress down; the store tier and the host registry running",
            out.getvalue(),
        )
        self.assertIn(("systemctl", "stop", "gideon-registry"), [call[0] for call in fake.calls])
        self.assertIn(("systemctl", "start", "gideon-registry"), [call[0] for call in fake.calls])

    def test_stores_render_this_hosts_inputs_before_starting_the_tier(self) -> None:
        fake = make_host()
        fake.files[os.fspath(nogpu.BUILD_BOX_PATH)] = "declared\n"
        checkout = Path("/work/GIDEON")
        lock = checkout / "input-host.lock"
        images = checkout / "input-images.lock"
        models = checkout / "input-models.lock"
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None), host=fake, now=NOW,
                root=checkout, lock_path=lock, images_path=images, models_path=models,
            )

        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(fake.events, ["registry-start", "render", "store-up"])
        self.assertEqual(fake.render_calls, [{
            "rendered_dir": RENDERED,
            "site_path": SITE_PATH,
            "lock_path": lock,
            "images_path": images,
            "models_path": models,
            "root": checkout,
            "diff": False,
            "command": "restore",
        }])

    def test_stores_refuse_a_render_failure_before_starting_the_tier(self) -> None:
        cases: tuple[tuple[RenderOutcome | Exception, str], ...] = (
            (
                RenderOutcome(
                    rendered=cast(RenderedSet, object()),
                    inputs=cast(RenderInputs, object()),
                    exit_code=2,
                ),
                "render inputs were refused",
            ),
            (RenderOutcome(exit_code=0), "render inputs were refused"),
            (RuntimeError("fictitious render failure"), "render for this host failed"),
        )
        for outcome, detail in cases:
            with self.subTest(outcome=outcome):
                fake = make_host()
                if isinstance(outcome, Exception):
                    fake.render_error = outcome
                else:
                    fake.render_outcome = outcome
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source="staging", at=None), host=fake, now=NOW,
                    )
                self.assertEqual(code, 1, out.getvalue())
                self.assertIn(f"stores: refuse — {detail}", out.getvalue())
                self.assertIn(apply.RENDER_FIX, out.getvalue())
                self.assertEqual(fake.events, ["render"])
                self.assertFalse(any("up" in argv for argv, _, _, _ in fake.calls))

    def test_undeclared_host_skips_host_registry_and_names_build_box(self) -> None:
        fake = make_host()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 0, out.getvalue())
        checkout = Path(restore.__file__).parents[2]
        self.assertEqual(fake.render_calls[0]["root"], checkout)
        self.assertEqual(fake.render_calls[0]["lock_path"], checkout / "host.lock")
        self.assertEqual(fake.render_calls[0]["images_path"], checkout / "images.lock")
        self.assertEqual(fake.render_calls[0]["models_path"], checkout / "models.lock")
        calls = [call[0] for call in fake.calls]
        self.assertNotIn(("systemctl", "stop", "gideon-registry"), calls)
        self.assertNotIn(("systemctl", "start", "gideon-registry"), calls)
        output = out.getvalue()
        self.assertIn(
            "stop: ok — stopped the GIDEON Compose project; no host registry: not the build box",
            output,
        )
        self.assertIn(
            "stores: ok — frontend and ingress down; the store tier running; qdrant: no snapshot in the set; opensearch: no snapshot in the set; level: no generation recorded; no host registry: not the build box",
            output,
        )
        self.assertIn(
            "next: ok — frontend and ingress down; the store tier running; no host registry: not the build box",
            output,
        )

    def test_no_gpu_host_also_skips_host_registry_and_names_build_box(self) -> None:
        fake = make_host()
        fake.files[os.fspath(nogpu.NO_GPU_PATH)] = "declared\n"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 0, out.getvalue())
        calls = [call[0] for call in fake.calls]
        self.assertNotIn(("systemctl", "stop", "gideon-registry"), calls)
        self.assertNotIn(("systemctl", "start", "gideon-registry"), calls)
        output = out.getvalue()
        self.assertIn(
            "stop: ok — stopped the GIDEON Compose project; no host registry: not the build box",
            output,
        )
        self.assertIn(
            "stores: ok — frontend and ingress down; the store tier running; qdrant: no snapshot in the set; opensearch: no snapshot in the set; level: no generation recorded; no host registry: not the build box",
            output,
        )
        self.assertIn(
            "next: ok — frontend and ingress down; the store tier running; no host registry: not the build box",
            output,
        )

    def test_present_derived_roots_recover_before_restore_audit(self) -> None:
        qdrant, opensearch = DERIVED_ROOTS
        fake = make_host(
            manifest_value=manifest_with_snapshots(qdrant, opensearch),
            qdrant_files=("alpha/alpha-20260902t110000z.snapshot",),
            opensearch_snapshots=(
                {"snapshot": "set-snapshot", "start_time_in_millis": 100, "state": "SUCCESS", "shards": {"failed": 0}, "indices": ["search"]},
            ),
        )
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None), host=fake, now=NOW,
            )

        self.assertEqual(code, 0, out.getvalue())
        stores_line = next(line for line in out.getvalue().splitlines() if line.startswith("stores:"))
        self.assertRegex(stores_line, r"qdrant: 1 collection\(s\) recovered in \d+\.\d s")
        self.assertRegex(stores_line, r"opensearch: 1 index\(es\) restored in \d+\.\d s")
        self.assertIn("; no host registry: not the build box", stores_line)
        self.assertIn(
            "next: ok — frontend and ingress down; the store tier running; no host registry: not the build box",
            out.getvalue(),
        )
        commands = [argv for argv, _, _, _ in fake.calls]
        qdrant_call = next(i for i, argv in enumerate(commands) if "exec" in argv and "qdrant" in argv)
        opensearch_call = next(i for i, argv in enumerate(commands) if "exec" in argv and "opensearch" in argv)
        audit_call = next(i for i, (_, request, _, _) in enumerate(fake.calls) if request and '"indexes"' in request)
        self.assertLess(qdrant_call, opensearch_call)
        self.assertLess(opensearch_call, audit_call)
        audit_text = fake.calls[audit_call][1]
        assert audit_text is not None
        self.assertIn('"indexes": {', audit_text)
        self.assertIn('"qdrant": {"names": ["alpha"]', audit_text)
        self.assertIn('"opensearch": {"names": ["search"]', audit_text)
        self.assertTrue(fake.running)

    def test_older_set_skips_both_derived_roots_without_store_children(self) -> None:
        fake = make_host()
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None), host=fake, now=NOW,
            )

        self.assertEqual(code, 0)
        self.assertIn("qdrant: no snapshot in the set; opensearch: no snapshot in the set", out.getvalue())
        self.assertFalse(any(
            "exec" in argv and ("qdrant" in argv or "opensearch" in argv)
            for argv, _, _, _ in fake.calls
        ))
        self.assertFalse(any(
            argv[:2] == ("find", DERIVED_ROOTS[0].source) and "*.snapshot" in argv
            for argv, _, _, _ in fake.calls
        ))
        audit_text = "\n".join(request or "" for _, request, _, _ in fake.calls)
        self.assertIn('"indexes": {', audit_text)
        self.assertIn('"qdrant": {"skipped": "no snapshot in the set"}', audit_text)
        self.assertIn('"opensearch": {"skipped": "no snapshot in the set"}', audit_text)

    def test_one_present_root_and_an_empty_directory_keep_the_other_skipped(self) -> None:
        qdrant, _ = DERIVED_ROOTS
        for files, expected in (
            (("alpha/alpha-20260902t110000z.snapshot",), "qdrant: 1 collection(s) recovered in "),
            ((), "qdrant: no snapshot in the set"),
        ):
            with self.subTest(files=files):
                fake = make_host(
                    manifest_value=manifest_with_snapshots(qdrant), qdrant_files=files,
                )
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source="staging", at=None), host=fake, now=NOW,
                    )
                self.assertEqual(code, 0, out.getvalue())
                self.assertIn(expected, out.getvalue())
                self.assertIn("opensearch: no snapshot in the set", out.getvalue())
                self.assertFalse(any("exec" in argv and "opensearch" in argv for argv, _, _, _ in fake.calls))
                self.assertIn(
                    ("find", qdrant.source, "-mindepth", "2", "-maxdepth", "2", "-type", "f", "-name", "*.snapshot", "-printf", "%P\\0"),
                    [argv for argv, _, _, _ in fake.calls],
                )

    def test_refused_derived_recovery_leaves_the_tier_running_without_audit(self) -> None:
        qdrant, _ = DERIVED_ROOTS
        fake = make_host(
            manifest_value=manifest_with_snapshots(qdrant),
            qdrant_files=("alpha/alpha-20260902t110000z.snapshot",),
            qdrant_status=403,
        )
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None), host=fake, now=NOW,
            )

        self.assertEqual(code, 1)
        self.assertTrue(fake.running)
        self.assertIn("stores: refuse", out.getvalue())
        self.assertIn("recovery refused", out.getvalue())
        self.assertIn(stack.logs_fix(RENDERED, "qdrant"), out.getvalue())
        self.assertNotIn("next:", out.getvalue())
        commands = [argv for argv, _, _, _ in fake.calls]
        tier_up = next(i for i, argv in enumerate(commands) if "up" in argv and "qdrant" in argv)
        self.assertFalse(any(argv[-1] == "down" for argv in commands[tier_up + 1:]))
        self.assertFalse(any(
            request and '"indexes"' in request for _, request, _, _ in fake.calls
        ))

    def test_refused_derived_recovery_keeps_the_replaced_staging(self) -> None:
        qdrant, _ = DERIVED_ROOTS
        fake = make_host(
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            manifest_value=manifest_with_snapshots(qdrant),
            qdrant_files=("alpha/alpha-20260902t110000z.snapshot",),
            qdrant_status=403,
        )
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None), host=fake, now=NOW,
            )

        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("stores: refuse", out.getvalue())
        replaced = f"{backupset.STAGING}.replaced-{REMOTE_SNAPSHOT}"
        self.assertTrue(any(argv[0] == "mv" and argv[-1] == replaced for argv, _, _, _ in fake.calls))
        self.assertFalse(any(argv == ("rm", "-rf", replaced) for argv, _, _, _ in fake.calls))

    def test_active_registry_on_an_undeclared_host_refuses_before_any_stage(self) -> None:
        fake = make_host(registry_active=True)
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(
            "the host registry is active, but this host is not declared the build box.",
            err.getvalue(),
        )
        self.assertIn(
            "Stop and disable gideon-registry (a former build box keeps its unit until removed by hand), or declare this host with host provision --build-box, then retry.",
            err.getvalue(),
        )
        calls = [call[0] for call in fake.calls]
        self.assertFalse(any(command[-1] == "down" for command in calls))
        self.assertNotIn(("systemctl", "stop", "gideon-registry"), calls)
        self.assertFalse(any(command[0] == "mv" for command in calls))

    def test_restore_excludes_the_build_box_marker_from_etc_gideon(self) -> None:
        fake = make_host()
        with contextlib.redirect_stdout(io.StringIO()):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 0)
        etc_rsync = next(
            call[0]
            for call in fake.calls
            if call[0][0] == "rsync" and call[0][-1] == "/etc/gideon/"
        )
        self.assertIn("--exclude=build-box", etc_rsync)

    def test_target_fetch_reowns_whole_side_and_swaps_after_verify(self) -> None:
        fake = make_host(
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            side_names=(LOCAL_SET,),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0)
        fetch = next(call[0] for call in fake.calls if call[0][0] == "rsync")
        self.assertEqual(
            fetch,
            (
                "rsync",
                "-aH",
                "--delete",
                "-e",
                sshtarget.rsync_ssh_option(),
                "gideon-backup@nas.test:/backup/snapshots/20260902T120000Z/",
                "/data/backup-staging.fetch-20260902T120000Z/",
            ),
        )
        self.assertTrue(
            any(call[0][:4] == ("chown", "-R", "-h", "1000:1001") for call in fake.calls)
        )
        self.assertTrue(
            any("/etc/gideon/config.yaml" in call[0] for call in fake.calls if call[0][0] == "chown")
        )
        self.assertTrue(
            any(
                call[0][:2]
                == ("find", "/data/backup-staging.fetch-20260902T120000Z/pgbackrest")
                for call in fake.calls
            )
        )
        self.assertTrue(any(call[0][:2] == ("mv", backupset.STAGING) for call in fake.calls))
        side = f"{backupset.STAGING}.fetch-{REMOTE_SNAPSHOT}"
        chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
        self.assertTrue(
            any(
                argv[1] == "1000:1000" and "/etc/gideon/config.yaml" in argv
                for argv in chowns
            )
        )
        self.assertIn(("chown", "gideon:gideon", "--", side), chowns)
        skeleton = [argv for argv in chowns if argv[1] == "0:0" and f"{side}/sets" in argv]
        self.assertTrue(skeleton, chowns)
        self.assertIn(f"{side}/sets/{LOCAL_SET}/files", skeleton[0])
        self.assertIn("frontend and ingress down", out.getvalue())

    def test_recorded_ids_are_mapped_in_fetch_and_files_for_target_and_staging(self) -> None:
        value = manifest()
        recorded = backupset.AccountIds(999, 983)
        additions = (
            entry("mapped-both", uid=999, gid=983),
            entry("mapped-uid", uid=999, gid=0),
            entry("mapped-gid", uid=0, gid=983),
            backupset.Entry("mapped-link", "l", 1, 999, 983, 0o777, 100.0, None),
        )
        value = replace(
            value,
            gideon_ids=recorded,
            inventory={
                **value.inventory,
                "etc-gideon": (*value.inventory["etc-gideon"], *additions),
            },
        )
        host_ids = backupset.AccountIds(998, 997)
        for source in ("target", "staging"):
            with self.subTest(source=source):
                fake = make_host(
                    source=source,
                    manifest_value=value,
                    gideon_ids=host_ids,
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                    side_names=(LOCAL_SET,),
                )
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source=source, at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 0, out.getvalue())
                output = out.getvalue()
                self.assertIn(
                    f"4 {restore.REOWN_MAPPED_DETAIL} {host_ids.uid}:{host_ids.gid}",
                    output,
                )
                chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
                self.assertIn(
                    ("chown", "998:997", "--", "/etc/gideon/mapped-both"),
                    chowns,
                )
                self.assertIn(
                    ("chown", "998:0", "--", "/etc/gideon/mapped-uid"),
                    chowns,
                )
                self.assertIn(
                    ("chown", "0:997", "--", "/etc/gideon/mapped-gid"),
                    chowns,
                )
                self.assertIn(
                    ("chown", "-h", "998:997", "--", "/etc/gideon/mapped-link"),
                    chowns,
                )

    def test_no_record_rows_and_next_line_name_host_provision(self) -> None:
        fake = make_host(
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            side_names=(LOCAL_SET,),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0, out.getvalue())
        output = out.getvalue()
        self.assertIn(
            f"re-owned 4 path(s), re-owned by their recorded ids "
            f"(1 set(s) with {restore.REOWN_NO_RECORD_DETAIL})",
            output,
        )
        self.assertIn(
            f"; re-owned by the recorded ids ({restore.REOWN_NO_RECORD_DETAIL})",
            output,
        )
        self.assertIn("Next: sudo python3 -m gideon host provision", output)
        self.assertIn("then sudo python3 -m gideon apply", output)

    def test_missing_gideon_account_refuses_before_select(self) -> None:
        fake = make_host(fail_gideon_ids=True)
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(any(call[0][0] == "docker" for call in fake.calls))
        self.assertIn(
            f"the {backupset.SERVICE_ACCOUNT} service account is missing or malformed.",
            err.getvalue(),
        )
        self.assertIn(
            "Run sudo python3 -m gideon host provision, then retry.",
            err.getvalue(),
        )

    def test_at_uses_the_oldest_remote_boundary_that_covers_it(self) -> None:
        early = backupset.PushRecord(
            "20260901T120000Z",
            NOW - timedelta(days=1),
            LOCAL_SET,
            NOW - timedelta(days=2),
        )
        late = backupset.PushRecord(
            REMOTE_SNAPSHOT,
            NOW,
            LOCAL_SET,
            NOW - timedelta(hours=1),
        )
        fake = make_host(
            source="target",
            remote_listing="20260901T120000Z\t1\n20260902T120000Z\t1\n",
            remote_record=late,
            remote_records={"20260901T120000Z": early, REMOTE_SNAPSHOT: late},
        )
        # The fake returns one record for every cat; exercise the pure choice
        # directly for the differing coverage boundaries.
        config = site.load_site(SITE_PATH, host=fake).config
        assert config is not None
        choice = restore._select_target(
            fake,
            config,
            NOW - timedelta(hours=2),
        )
        self.assertTrue(choice[0].ok)
        assert choice[1] is not None
        assert choice[1].snapshot is not None
        self.assertEqual(choice[1].snapshot.label, REMOTE_SNAPSHOT)
        self.assertEqual(early.newest_set, LOCAL_SET)

    def test_pre_restore_runs_only_when_postgres_is_ready_and_pushes_target(self) -> None:
        for source in ("staging", "target"):
            with self.subTest(source=source):
                fake = make_host(
                    running=True,
                    source=source,
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                    side_names=(LOCAL_SET,),
                )
                calls: list[tuple[str, object]] = []

                def record_run(
                    *args: object,
                    _calls: list[tuple[str, object]] = calls,
                    **kwargs: object,
                ) -> int:
                    del args
                    _calls.append(("run", kwargs))
                    return 0

                def record_push(
                    *args: object,
                    _calls: list[tuple[str, object]] = calls,
                    **kwargs: object,
                ) -> int:
                    del args
                    _calls.append(("push", kwargs))
                    return 0

                with (
                    patch.object(
                        restore.backup,
                        "run_backup_run",
                        side_effect=record_run,
                    ),
                    patch.object(
                        restore.backup,
                        "run_backup_push",
                        side_effect=record_push,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    code = restore.run_restore(
                        argparse.Namespace(source=source, at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 0)
                self.assertEqual([kind for kind, _ in calls], ["run", "push"] if source == "target" else ["run"])
                run_kwargs = calls[0][1]
                assert isinstance(run_kwargs, dict)
                self.assertTrue(run_kwargs["pre_restore"])
                self.assertEqual(run_kwargs["now"], NOW)

    def test_pre_restore_nested_runs_keep_the_restore_lock(self) -> None:
        for source in ("staging", "target"):
            with self.subTest(source=source):
                fake = make_host(
                    running=True,
                    source=source,
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                    side_names=(LOCAL_SET,),
                )
                outer_records: list[str] = []
                nested_returns: list[str] = []
                original_take_lock = fake.take_lock

                def observe_lock(
                    path: PathLike,
                    record: str,
                    *,
                    _original_take_lock: Any = original_take_lock,
                    _outer_records: list[str] = outer_records,
                    _nested_returns: list[str] = nested_returns,
                ) -> str | None:
                    result = _original_take_lock(path, record)
                    if result is None:
                        _outer_records.append(record)
                    else:
                        _nested_returns.append(result)
                    return result

                def nested_run(
                    *args: object,
                    _fake: FakeHost = fake,
                    _outer_records: list[str] = outer_records,
                    **kwargs: object,
                ) -> int:
                    del args, kwargs
                    outcome = backuplock.take(_fake, command="backup run", now=NOW)
                    self.assertEqual(outcome.state, backuplock.State.NESTED)
                    self.assertEqual(
                        _fake.locks[backuplock.BACKUP_LOCK.path], _outer_records[0]
                    )
                    return 0

                def nested_push(
                    *args: object,
                    _fake: FakeHost = fake,
                    _outer_records: list[str] = outer_records,
                    **kwargs: object,
                ) -> int:
                    del args, kwargs
                    outcome = backuplock.take(_fake, command="backup push", now=NOW)
                    self.assertEqual(outcome.state, backuplock.State.NESTED)
                    self.assertEqual(
                        _fake.locks[backuplock.BACKUP_LOCK.path], _outer_records[0]
                    )
                    return 0

                with (
                    patch.object(fake, "take_lock", side_effect=observe_lock),
                    patch.object(restore.backup, "run_backup_run", side_effect=nested_run),
                    patch.object(restore.backup, "run_backup_push", side_effect=nested_push),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    code = restore.run_restore(
                        argparse.Namespace(source=source, at=None),
                        host=fake,
                        now=NOW,
                    )

                self.assertEqual(code, 0)
                self.assertEqual(len(outer_records), 1)
                record = backuplock.parse(outer_records[0])
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual(record.command, "restore")
                self.assertEqual(record.started, NOW)
                self.assertEqual(
                    len(nested_returns), 2 if source == "target" else 1
                )
                self.assertTrue(all(value == outer_records[0] for value in nested_returns))
                self.assertNotIn(backuplock.BACKUP_LOCK.path, fake.locks)

    def test_fresh_target_skips_pre_restore_for_absent_and_empty_sets(self) -> None:
        for sets_absent in (True, False):
            with self.subTest(sets_absent=sets_absent):
                fake = make_host(
                    running=True,
                    source="target",
                    names=(),
                    sets_absent=sets_absent,
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                )
                out = io.StringIO()
                with (
                    patch.object(
                        restore.backup,
                        "run_backup_run",
                        side_effect=AssertionError("fresh stack must not take a set"),
                    ),
                    patch.object(
                        restore.backup,
                        "run_backup_push",
                        side_effect=AssertionError("fresh stack must not push a set"),
                    ),
                    contextlib.redirect_stdout(out),
                ):
                    code = restore.run_restore(
                        argparse.Namespace(source="target", at=None),
                        host=fake,
                        now=NOW,
                    )

                self.assertEqual(code, 0, out.getvalue())
                output = out.getvalue()
                self.assertIn(
                    f"pre-restore: ok — {restore.FRESH_STACK_SKIPPED_DETAIL}",
                    output,
                )
                self.assertIn("fetch: ok", output)
                audit_sql = "\n".join(call[1] or "" for call in fake.calls)
                self.assertIn('"pre_restore_label": null', audit_sql)

    def test_incomplete_staging_refuses_before_backup_or_down(self) -> None:
        partial_label = LOCAL_SET + backupset.PARTIAL_SUFFIX
        cases = ((partial_label, None), (LOCAL_SET, "{not a manifest"))
        for label, malformed_manifest in cases:
            with self.subTest(label=label):
                fake = make_host(
                    running=True,
                    source="target",
                    names=(label,),
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                )
                if malformed_manifest is not None:
                    fake.files[
                        os.path.join(
                            backupset.set_dir(LOCAL_SET), backupset.MANIFEST_NAME
                        )
                    ] = malformed_manifest
                out = io.StringIO()
                with (
                    patch.object(
                        restore.backup,
                        "run_backup_run",
                        side_effect=AssertionError("incomplete staging must not take a set"),
                    ),
                    patch.object(
                        restore.backup,
                        "run_backup_push",
                        side_effect=AssertionError("incomplete staging must not push a set"),
                    ),
                    contextlib.redirect_stdout(out),
                ):
                    code = restore.run_restore(
                        argparse.Namespace(source="target", at=None),
                        host=fake,
                        now=NOW,
                    )

                self.assertEqual(code, 1)
                output = out.getvalue()
                pre_restore = next(
                    line for line in output.splitlines() if line.startswith("pre-restore:")
                )
                self.assertIn(
                    f"no complete backup set in staging, only 1 incomplete entry: {label}",
                    pre_restore,
                )
                self.assertNotIn(backupset.SETS_DIR, pre_restore.split(" Fix:", 1)[0])
                self.assertIn(
                    "Run sudo python3 -m gideon backup run to complete a set",
                    output,
                )
                self.assertIn(
                    "stop the stack with docker compose -f /etc/gideon/rendered/compose.yaml down",
                    output,
                )
                self.assertFalse(
                    any(
                        call[0][:2] == ("docker", "compose")
                        and call[0][-1] == "down"
                        for call in fake.calls
                    )
                )

    def test_pre_restore_push_failure_refuses_before_down(self) -> None:
        fake = make_host(
            running=True,
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        out = io.StringIO()
        with (
            patch.object(restore.backup, "run_backup_run", return_value=0),
            patch.object(restore.backup, "run_backup_push", return_value=1),
            contextlib.redirect_stdout(out),
        ):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 1)
        self.assertIn("pre-restore push failed", out.getvalue())
        self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))
        self.assertNotIn(backuplock.BACKUP_LOCK.path, fake.locks)

    def test_partly_running_fresh_stack_skips_pre_restore(self) -> None:
        fake = make_host(
            running=True,
            partial=True,
            source="target",
            names=(),
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        out = io.StringIO()
        with (
            patch.object(
                restore.backup,
                "run_backup_run",
                side_effect=AssertionError("fresh stack must not take a set"),
            ),
            patch.object(
                restore.backup,
                "run_backup_push",
                side_effect=AssertionError("fresh stack must not push a set"),
            ),
            contextlib.redirect_stdout(out),
        ):
            restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        # The partial refusal protects a safety set a fresh stack cannot have,
        # so the skip wins and the later stop stage runs down itself.
        output = out.getvalue()
        self.assertIn(
            f"pre-restore: ok — {restore.FRESH_STACK_SKIPPED_DETAIL}",
            output,
        )
        self.assertNotIn("partially running", output)

    def test_partly_running_incomplete_staging_refuses_as_partial(self) -> None:
        fake = make_host(
            running=True,
            partial=True,
            source="target",
            names=(LOCAL_SET + backupset.PARTIAL_SUFFIX,),
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 1)
        output = out.getvalue()
        self.assertIn("partially running (open-webui)", output)
        self.assertNotIn("no complete backup set in staging", output)

    def test_stopped_fresh_stack_keeps_the_existing_skip_row(self) -> None:
        fake = make_host(
            running=False,
            source="target",
            names=(),
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("pre-restore: ok — skipped (stack not running)", out.getvalue())

    def test_unlistable_staging_refuses_with_the_backup_run_fix(self) -> None:
        fake = make_host(
            running=True,
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
        )
        listable = fake.listdir

        def deny_sets(path: PathLike) -> list[str]:
            if os.fspath(path) == backupset.SETS_DIR:
                raise PermissionError("permission denied")
            return listable(path)

        out = io.StringIO()
        with (
            patch.object(fake, "listdir", side_effect=deny_sets),
            contextlib.redirect_stdout(out),
        ):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )

        self.assertEqual(code, 1)
        output = out.getvalue()
        self.assertIn("cannot list backup sets: permission denied", output)
        self.assertIn("Run sudo python3 -m gideon backup run, then retry.", output)

    def test_staging_at_after_refreshed_archive_boundary_refuses_before_down(self) -> None:
        fake = make_host()
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at="2026-09-02T11:30Z"),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertIn("newest local archive boundary", out.getvalue())
        self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))

    def test_at_reaches_pgbackrest_as_an_explicit_time_target(self) -> None:
        fake = make_host()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at="2026-09-02T11:00Z"),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0)
        restore_call = next(
            call[0]
            for call in fake.calls
            if call[0][0:2] == ("docker", "compose")
            and "pgbackrest" in call[0]
            and "restore" in call[0]
        )
        self.assertIn("--type=time", restore_call)
        self.assertIn("--target=2026-09-02 11:00:00+00:00", restore_call)
        self.assertIn("--target-action=promote", restore_call)
        # Every set restore names its own backup and that backup's timeline:
        # auto-selection and the current timeline both go wrong after a fork.
        self.assertIn(f"--set={manifest().pgbackrest_label}", restore_call)
        self.assertIn("--target-timeline=2", restore_call)

    def test_target_at_after_newest_remote_boundary_refuses_in_select(self) -> None:
        fake = make_host(
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            remote_record=backupset.PushRecord(
                REMOTE_SNAPSHOT,
                NOW,
                LOCAL_SET,
                NOW - timedelta(hours=1),
            ),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source="target", at="2026-09-02T11:30Z"),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 1)
        self.assertIn("newest remote archive boundary", out.getvalue())
        self.assertIn(SELECT_FIX, out.getvalue())

    def test_target_select_row_names_the_boundary_and_newer_unfinished_snapshots(self) -> None:
        boundary = manifest().archive_through.isoformat()
        removing_names = (
            "20260902T150000Z.removing",
            "20260902T160000Z.partial.removing",
            "pre-example.removing",
        )
        removing_listing = "".join(f"{name}\t1\n" for name in removing_names)
        listings = {
            "unfinished": (
                f"{REMOTE_SNAPSHOT}\t1\n20260902T130000Z\t0\n"
                "20260902T140000Z.partial\t1\n20260901T120000Z.partial\t0\n"
            ),
            "complete": f"{REMOTE_SNAPSHOT}\t1\n",
            "removing": f"{REMOTE_SNAPSHOT}\t1\n" + removing_listing,
        }
        listings["mixed"] = listings["unfinished"] + removing_listing
        for name, listing in listings.items():
            with self.subTest(listing=name):
                fake = make_host(
                    source="target",
                    remote_listing=listing,
                    side_names=(LOCAL_SET,),
                )
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source="target", at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 0)
                row = next(
                    line for line in out.getvalue().splitlines() if line.startswith("select:")
                )
                expected = (
                    f"source=target; snapshot={REMOTE_SNAPSHOT}; set=chosen after fetch; "
                    f"at=latest; archive_through={boundary}"
                )
                if name in {"unfinished", "mixed"}:
                    expected += (
                        "; 2 newer unfinished snapshot(s): "
                        "20260902T130000Z, 20260902T140000Z.partial"
                    )
                self.assertTrue(row.endswith(expected), row)
                for removing_name in removing_names:
                    self.assertNotIn(removing_name, row)

    def test_target_select_runs_before_pre_restore_and_never_takes_its_push(self) -> None:
        fake = make_host(
            running=True,
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            side_names=(LOCAL_SET,),
        )
        listings_at_push: list[int] = []

        def listings() -> int:
            return sum(
                1
                for call in fake.calls
                if call[0][0] == "ssh" and "for directory in */" in call[0][-1]
            )

        def push(*args: object, **kwargs: object) -> int:
            del args, kwargs
            listings_at_push.append(listings())
            fake.remote_listing += "20260902T121000Z\t1\n"
            return 0

        out = io.StringIO()
        with (
            patch.object(restore.backup, "run_backup_run", return_value=0),
            patch.object(restore.backup, "run_backup_push", side_effect=push),
            contextlib.redirect_stdout(out),
        ):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0)
        self.assertEqual(listings_at_push, [1])
        self.assertEqual(listings(), 1)
        rows = [line.split(":", 1)[0] for line in out.getvalue().splitlines()]
        self.assertLess(rows.index("select"), rows.index("pre-restore"))
        fetch = next(call[0] for call in fake.calls if call[0][0] == "rsync")
        self.assertIn(f"/backup/snapshots/{REMOTE_SNAPSHOT}/", fetch[-2])

    def test_verify_skips_the_repository_info_files_and_names_the_root(self) -> None:
        fake = make_host()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW), 0)
        repository_checks = [
            call[1] or ""
            for call in fake.calls
            if call[0][:2] == ("sha256sum", "-c") and call[2] == "/data/backup-staging/pgbackrest"
        ]
        self.assertEqual(len(repository_checks), 1)
        self.assertNotIn("backup.info", repository_checks[0])
        self.assertNotIn("archive.info", repository_checks[0])
        self.assertIn("backup/gideon/backup-label/backup.manifest", repository_checks[0])
        failing = make_host(fail_checksum=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            restore.run_restore(argparse.Namespace(source="staging", at=None), host=failing, now=NOW)
        self.assertIn("path(s) in etc-gideon", out.getvalue())

    def test_verify_and_pgbackrest_refusals_happen_before_down(self) -> None:
        for failure in ("checksum", "pgbackrest"):
            with self.subTest(failure=failure):
                fake = make_host(
                    fail_checksum=failure == "checksum",
                    fail_pgbackrest=failure == "pgbackrest",
                )
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source="staging", at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 1)
                self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))
                self.assertIn("Fix:", out.getvalue())

    def test_fetch_falls_back_to_one_off_identity_lookup(self) -> None:
        fake = make_host(
            source="target",
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            side_names=(LOCAL_SET,),
            fail_identity_exec=True,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = restore.run_restore(
                argparse.Namespace(source="target", at=None),
                host=fake,
                now=NOW,
            )
        self.assertEqual(code, 0)
        identity_calls = [
            call[0]
            for call in fake.calls
            if call[0][:3] == ("docker", "compose", "--project-directory")
            and "run" in call[0]
            and "id" in call[0]
        ]
        self.assertEqual(len(identity_calls), 2)

    def test_partial_stack_refuses_before_anything_stops(self) -> None:
        fake = make_host(running=True, partial=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("partially running (open-webui)", out.getvalue())
        self.assertIn("gideon apply", out.getvalue())
        self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))

    def test_swap_failure_moves_the_live_staging_back(self) -> None:
        fake = make_host(source="target", remote_listing=f"{REMOTE_SNAPSHOT}\t1\n", fail_swap=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="target", at=None), host=fake, now=NOW)
        self.assertEqual(code, 1)
        moves = [call[0] for call in fake.calls if call[0][0] == "mv"]
        self.assertEqual(moves[-1], ("mv", f"{backupset.STAGING}.replaced-{REMOTE_SNAPSHOT}", backupset.STAGING))
        self.assertIn("moved back", out.getvalue())
        self.assertFalse(any("pgbackrest" in call[0] and "restore" in call[0] for call in fake.calls))

    def test_symlink_entries_are_reowned_without_dereferencing(self) -> None:
        value = manifest()
        link = backupset.Entry("link-to-elsewhere", "l", 1, 1000, 1000, 0o777, 100.0, None)
        with_link = replace(value, inventory={**value.inventory, "etc-gideon": (*value.inventory["etc-gideon"], link)})
        fake = make_host(manifest_value=with_link)
        with contextlib.redirect_stdout(io.StringIO()):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 0)
        chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
        self.assertIn(("chown", "-h", "1000:1000", "--", "/etc/gideon/link-to-elsewhere"), chowns)
        chmods = [call[0] for call in fake.calls if call[0][0] == "chmod"]
        self.assertFalse(any("/etc/gideon/link-to-elsewhere" in argv for argv in chmods))

    def test_a_path_beneath_a_link_refuses_before_any_metadata_is_applied(self) -> None:
        value = manifest()
        with_escape = replace(
            value,
            inventory={
                **value.inventory,
                "etc-gideon": (*value.inventory["etc-gideon"], entry("escape/passwd")),
            },
        )
        for source, listing in (("staging", ""), ("target", f"{REMOTE_SNAPSHOT}\t1\n")):
            with self.subTest(source=source):
                fake = make_host(source=source, remote_listing=listing, manifest_value=with_escape, physical_escape=True)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(argparse.Namespace(source=source, at=None), host=fake, now=NOW)
                self.assertEqual(code, 1)
                self.assertIn("beneath a link", out.getvalue())
                self.assertFalse(any("escape/passwd" in call[0] for call in fake.calls if call[0][0] in ("chown", "chmod")))

    def test_a_root_without_a_dot_entry_keeps_the_live_directory_owner(self) -> None:
        fake = make_host()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW), 0)
        chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
        self.assertIn(("chown", "999:983", "--", "/data/registry"), chowns)
        chmods = [call[0] for call in fake.calls if call[0][0] == "chmod"]
        self.assertIn(("chmod", "0755", "--", "/data/registry"), chmods)

        value = manifest()
        dotted = replace(
            value,
            inventory={
                **value.inventory,
                "data-registry": (
                    backupset.Entry(backupset.ROOT_ENTRY, "d", 4096, 999, 983, 0o755, 100.0, None),
                    *value.inventory["data-registry"],
                ),
            },
        )
        fake = make_host(manifest_value=dotted)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW), 0)
        chowns = [call[0] for call in fake.calls if call[0][0] == "chown"]
        self.assertNotIn(("chown", "999:983", "--", "/data/registry"), chowns)
        self.assertIn(("chown", "999:983", "--", "/data/registry/."), chowns)

    def test_a_path_the_manifest_never_inventoried_refuses_at_verify_before_stop(self) -> None:
        """A snapshotted root is the manifest's set and nothing else, an empty root included."""

        value = manifest()
        empty_registry = replace(value, inventory={**value.inventory, "data-registry": ()})
        fake = make_host(manifest_value=empty_registry, extra_physical_file=("data-registry", "stray.blob"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("verify: refuse — 1 path(s) under", out.getvalue())
        self.assertIn("are not in the manifest: stray.blob", out.getvalue())
        self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))

    def test_a_root_with_no_files_is_verified_without_a_checksum_call(self) -> None:
        """/data/registry is empty on a no-GPU host; sha256sum -c refuses an empty list."""

        value = manifest()
        empty_registry = replace(value, inventory={**value.inventory, "data-registry": ()})
        fake = make_host(manifest_value=empty_registry)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 0, out.getvalue())
        registry_checks = [
            call for call in fake.calls
            if call[0][:2] == ("sha256sum", "-c") and str(call[2]).endswith("/data-registry")
        ]
        self.assertEqual(registry_checks, [])

    def test_refusals_and_secret_kept_line(self) -> None:
        for fake, expected in (
            (make_host(), "--from staging"),
            (make_host(target=False, source="target"), "docs/runbooks/office-services-setup.md §3"),
            (make_host(secrets_present=True), "secrets on disk are the set's"),
        ):
            if expected == "--from staging":
                args = argparse.Namespace(source=None, at=None)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = restore.run_restore(args, host=fake, now=NOW)
                self.assertEqual(code, 1)
                self.assertIn(expected, err.getvalue())
            elif expected == "docs/runbooks/office-services-setup.md §3":
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = restore.run_restore(
                        argparse.Namespace(source="target", at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 1)
                self.assertIn(expected, err.getvalue())
            else:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = restore.run_restore(
                        argparse.Namespace(source="staging", at=None),
                        host=fake,
                        now=NOW,
                    )
                self.assertEqual(code, 0)
                self.assertIn(expected, out.getvalue())


if __name__ == "__main__":
    unittest.main()


class IndexRestore(RenderedRestoreCase):
    """Recovery is levelled before staging cleanup, audit, and the next commands."""

    def _run(self, fake: FakeHost) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(
                argparse.Namespace(source=fake.source, at=None), host=fake, now=NOW,
            )
        return code, out.getvalue()

    def _detail(self, fake: FakeHost) -> Mapping[str, Any]:
        sql = next(
            request for _, request, _, _ in fake.calls
            if request and "\\set v_kind 'restore'" in request
        )
        line = next(line for line in sql.splitlines() if line.startswith("\\set v_detail '"))
        return json.loads(line.removeprefix("\\set v_detail '").removesuffix("'"))

    def test_level_clauses_and_audit_follow_recovery_before_replaced_staging_cleanup(self) -> None:
        live = generation("live")
        built = generation("built", label="corpus-2099-02-03")
        rows = (live, built)
        lexical = {row.gen_id: row.counts["indexed"] + row.counts["present"] for row in rows}
        dense = {row.gen_id: row.counts["dense_total"] for row in rows}
        dense[built.gen_id] -= 1
        fake = make_host(
            source="target", remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
            manifest_value=manifest_with_snapshots(*DERIVED_ROOTS),
            qdrant_files=tuple(f"{row.gen_id}/restored.snapshot" for row in rows),
            opensearch_snapshots=({
                "snapshot": "fictitious-snapshot", "start_time_in_millis": 100,
                "state": "SUCCESS", "shards": {"failed": 0},
                "indices": [row.gen_id for row in rows],
            },),
            generations=tuple(asdict(row) for row in rows),
            index_counts={"opensearch": lexical, "qdrant": dense},
            index_aliases={"opensearch": {live.collection: (built.gen_id,)}},
        )
        fake.files[os.fspath(nogpu.BUILD_BOX_PATH)] = "declared\n"
        fake.files[os.path.join(RENDERED, "compose.yaml")] = POSTGRES_COMPOSE

        code, output = self._run(fake)

        self.assertEqual(code, 0, output)
        stores_line = next(line for line in output.splitlines() if line.startswith("stores:"))
        clauses = stores_line.split("; ")
        recovery_end = next(i for i, clause in enumerate(clauses) if clause.startswith("opensearch:"))
        level_clauses = [clause for clause in clauses if clause.startswith("level ")]
        self.assertEqual(clauses[recovery_end + 1:-1], level_clauses)
        self.assertEqual(len(level_clauses), len(rows))
        self.assertTrue(level_clauses[0].startswith(f"level {live.gen_id}: live, whole"))
        self.assertIn(f"level {built.gen_id}: built -> building", level_clauses[1])
        self.assertIn(f"short in qdrant ({dense[built.gen_id]} of {built.counts['dense_total']})", level_clauses[1])
        self.assertIn("alias lexical stands", level_clauses[1])
        self.assertEqual(clauses[-1], "the host registry running")
        for clause in level_clauses:
            self.assertTrue(any(pattern.fullmatch(clause) for pattern in level.level_clause_patterns()), clause)
        expected_level = {
            row.gen_id: {
                "state": row.state, "outcome": "aliased" if row == live else "demoted",
                "lexical_count": lexical[row.gen_id], "lexical_expected": lexical[row.gen_id],
                "dense_count": dense[row.gen_id], "dense_expected": row.counts["dense_total"],
                "lexical_alias": f"{built.gen_id} -> {live.gen_id}" if row == live else "stands",
                "dense_alias": f"none -> {live.gen_id}" if row == live else None,
            }
            for row in rows
        }
        detail = self._detail(fake)
        self.assertEqual(detail["level"], expected_level)
        self.assertTrue(detail["replaced_removed"])
        self.assertEqual(fake.generations[0], asdict(live))
        self.assertEqual(fake.generations[1]["state"], "building")
        self.assertEqual(len(fake.generation_statements), 2)
        calls = fake.calls
        recovery = max(i for i, (_, request, _, _) in enumerate(calls)
                       if request and ("/snapshots/recover?" in request or "_restore?wait_for_completion" in request))
        read = next(i for i, (argv, request, _, _) in enumerate(calls)
                    if argv == tuple(worker.psql_argv(RENDERED)) and request and "row_to_json" in request)
        demotion = next(i for i, (_, request, _, _) in enumerate(calls) if request and "DO $demotion$" in request)
        alias_write = next(i for i, (_, request, _, _) in enumerate(calls)
                           if request and 'request = "POST"' in request and "/_aliases" in request)
        replaced = f"{backupset.STAGING}.replaced-{REMOTE_SNAPSHOT}"
        removed = next(i for i, (argv, _, _, _) in enumerate(calls) if argv == ("rm", "-rf", replaced))
        audit_call = next(i for i, (_, request, _, _) in enumerate(calls) if request and "\\set v_kind 'restore'" in request)
        self.assertLess(recovery, read)
        self.assertLess(read, demotion)
        self.assertLess(demotion, alias_write)
        self.assertLess(alias_write, removed)
        self.assertLess(removed, audit_call)
        self.assertTrue(fake.running)
        for argv, request, _, _ in calls:
            if argv == tuple(worker.psql_argv(RENDERED)):
                self.assertIsNotNone(request)
                self.assertNotIn(live.gen_id, " ".join(argv))

    def test_level_refusals_keep_the_tier_and_replaced_staging_without_restore_audit(self) -> None:
        row = generation("live")
        cases = (
            ({"generation_read_code": 3}, database_fix(RENDERED, "restore")),
            ({"generation_demotion_code": 3}, database_fix(RENDERED, "restore")),
            ({"level_store_code": 127}, stack.logs_fix(RENDERED, "opensearch")),
        )
        for failures, fix in cases:
            with self.subTest(failures=failures):
                fake = make_host(
                    source="target", remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                    manifest_value=manifest_with_snapshots(DERIVED_ROOTS[0]),
                    qdrant_files=(f"{row.gen_id}/restored.snapshot",),
                    generations=(asdict(row),),
                    index_counts={
                        "opensearch": {row.gen_id: row.counts["indexed"] + row.counts["present"]},
                        "qdrant": {row.gen_id: 0},
                    },
                    **failures,
                )

                code, output = self._run(fake)

                self.assertEqual(code, 1, output)
                self.assertIn("stores: refuse", output)
                self.assertIn(f"Fix: {fix}", output)
                self.assertNotIn("next:", output)
                self.assertNotIn("Next:", output)
                self.assertTrue(fake.running)
                replaced = f"{backupset.STAGING}.replaced-{REMOTE_SNAPSHOT}"
                commands = [argv for argv, _, _, _ in fake.calls]
                self.assertTrue(any(argv[0] == "mv" and argv[-1] == replaced for argv in commands))
                self.assertNotIn(("rm", "-rf", replaced), commands)
                tier = next(i for i, argv in enumerate(commands) if argv == tuple(stack.compose_argv(RENDERED, "up", "-d", *STORE_SERVICES)))
                self.assertFalse(any(argv[-1] == "down" for argv in commands[tier + 1:]))
                self.assertFalse(any(request and "\\set v_kind 'restore'" in request for _, request, _, _ in fake.calls))

    def test_empty_generation_listing_keeps_next_commands_and_audits_an_empty_level(self) -> None:
        fake = make_host()
        code, output = self._run(fake)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(fake.generation_statements), 1)
        self.assertIn("SELECT row_to_json(generation)", fake.generation_statements[0])
        self.assertIn("; level: no generation recorded; ", output)
        self.assertEqual(self._detail(fake)["level"], {})
        self.assertTrue(output.rstrip().endswith(
            "then sudo python3 -m gideon apply, then sudo python3 -m gideon backup run --full"
        ))

    def test_next_commands_resume_each_label_then_promote_in_both_next_shapes(self) -> None:
        rows = (generation("built"), generation("live", label="corpus-2099-02-03"))
        self.addCleanup(report.set_installed_form, False)
        report.set_installed_form(False)
        for has_ids in (False, True):
            with self.subTest(has_ids=has_ids):
                value = manifest()
                if has_ids:
                    value = replace(value, gideon_ids=DEFAULT_GIDEON_IDS)
                fake = make_host(
                    manifest_value=value, generations=tuple(asdict(row) for row in rows),
                    index_counts={
                        "opensearch": {row.gen_id: row.counts["indexed"] + row.counts["present"] for row in rows},
                        "qdrant": {row.gen_id: None for row in rows},
                    },
                )
                code, output = self._run(fake)
                self.assertEqual(code, 0, output)
                commands = [
                    "sudo python3 -m gideon apply",
                    *(f"sudo python3 -m gideon index build {row.collection} --lockfile {row.corpus_lockfile}" for row in rows),
                    f"sudo python3 -m gideon index promote {rows[1].gen_id}",
                    "sudo python3 -m gideon backup run --full",
                ]
                prefix = "Next: " if has_ids else (
                    "Next: sudo python3 -m gideon host provision (the first provision's mode flags), "
                    "because this set records no gideon ids, then "
                )
                self.assertEqual(
                    [line for line in output.splitlines() if line.startswith("Next:")],
                    [prefix + ", then ".join(commands)],
                )


class StoreRestoreCommands(RenderedRestoreCase):
    def _run(self, fake: FakeHost, source: str, *, at: datetime | None = None) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = restore.run_restore(
                argparse.Namespace(source=source, at=at.isoformat() if at is not None else None),
                host=fake, now=NOW,
            )
        return code, output.getvalue()

    def _audit_text(self, fake: FakeHost) -> str:
        return "\n".join(call[1] or "" for call in fake.calls)

    def test_pinned_store_adds_a_missing_object_without_naming_a_live_only_one(self) -> None:
        added = hashlib.sha256(b"copy bytes").hexdigest()
        live_only = hashlib.sha256(b"live only bytes").hexdigest()
        value, listing = manifest_with_store({added: len(b"copy bytes")})
        for source in ("staging", "target"):
            with self.subTest(source=source):
                fake = make_host(
                    source=source, manifest_value=value, store_listing=listing,
                    store_live={live_only: len(b"live only bytes")},
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n" if source == "target" else "",
                )
                code, output = self._run(fake, source)

                self.assertEqual(code, 0, output)
                self.assertIn(
                    f"{STORE_ROOT.name}: added 1 object(s), {backuproots.NOTHING_LEFT_OUT_DETAIL}", output,
                )
                transfers = [call for call in fake.calls if "--files-from=-" in call[0]]
                self.assertEqual(len(transfers), 1)
                self.assertNotIn("--delete", transfers[0][0])
                store_commands = [
                    call[0] for call in fake.calls
                    if any(os.fspath(STORE_ROOT.source) in arg or "/files/" + STORE_ROOT.name in arg for arg in call[0])
                ]
                self.assertFalse(any("--delete" in argv for argv in store_commands))
                self.assertEqual(transfers[0][1], f"{added[:2]}/{added[2:4]}/{added}\n")
                self.assertFalse(any(live_only in str(call) for call in fake.calls))
                self.assertEqual(fake.store_live, {
                    live_only: len(b"live only bytes"), added: len(b"copy bytes"),
                })
                self.assertIn(
                    f'"store": {{"{STORE_ROOT.name}": {{"added": 1, "left_out": 0}}}}',
                    self._audit_text(fake),
                )

    def test_staging_restore_reads_each_manifest_form_and_names_a_damaged_object(self) -> None:
        data = (b"fictitious intact object", b"fictitious damaged object")
        sizes = {hashlib.sha256(content).hexdigest(): len(content) for content in data}
        damaged = hashlib.sha256(data[1]).hexdigest()
        live_only = hashlib.sha256(b"fictitious live object").hexdigest()
        original, listing = manifest_with_store(sizes)
        for version in (1, backupset.MANIFEST_VERSION):
            for fail_object in (False, True):
                with self.subTest(version=version, damaged=fail_object):
                    value = replace(original, version=version)
                    fake = make_host(manifest_value=value, store_listing=listing,
                                     store_live={live_only: 9}, store_damaged=(damaged,) if fail_object else ())
                    code, output = self._run(fake, "staging")
                    self.assertEqual(code, 0, output)
                    copy = backupset.store_copy(backupset.STAGING, backupset.set_dir(LOCAL_SET), value, STORE_ROOT.name)
                    checks = [call for call in fake.calls if call[0] == ("sha256sum", "-c", "-") and call[2] == copy]
                    self.assertEqual(checks, [(
                        ("sha256sum", "-c", "-"),
                        "".join(f"{name}  {name[:2]}/{name[2:4]}/{name}\n" for name in sorted(sizes)), copy, None,
                    )])
                    passing = sorted(name for name in sizes if not fail_object or name != damaged)
                    transfers = [call for call in fake.calls if "--files-from=-" in call[0]]
                    self.assertEqual(transfers, [(
                        ("rsync", "-rtp", "--stats", f"--chmod=D{cas.DIRECTORY_MODE:o},F{cas.OBJECT_MODE:o}",
                         "--ignore-existing", "--files-from=-", copy + "/", os.fspath(cas.ROOT) + "/"),
                        "".join(f"{name[:2]}/{name[2:4]}/{name}\n" for name in passing), None, None,
                    )])
                    self.assertEqual(fake.store_live, {live_only: 9, **{name: sizes[name] for name in passing}})
                    self.assertIn(f"{STORE_ROOT.name}: added {len(passing)} object(s), {int(fail_object)} left out", output)
                    if fail_object:
                        closing = next(line for line in output.splitlines() if line.startswith("Store:"))
                        self.assertIn(damaged, closing)
                    else:
                        self.assertNotIn("Store:", output)

    def test_set_without_store_pin_leaves_live_store_alone(self) -> None:
        live_only = hashlib.sha256(b"live only bytes").hexdigest()
        fake = make_host(store_live={live_only: 15})
        code, output = self._run(fake, "staging")

        self.assertEqual(code, 0, output)
        self.assertIn(f"{STORE_ROOT.name}: the set predates this root; the live store is left as it is", output)
        self.assertEqual(fake.store_live, {live_only: 15})
        self.assertFalse(any(call[0][:3] == ("env", "LC_ALL=C", "join") for call in fake.calls))
        self.assertFalse(any(call[0][:2] == ("sha256sum", "-c") and call[2] is not None and call[2].endswith("/files/" + STORE_ROOT.name) for call in fake.calls))
        self.assertFalse(any(call[0][0] == "rsync" and call[0][-1] == os.fspath(cas.ROOT) + "/" for call in fake.calls))
        self.assertFalse(any(call[2] == os.fspath(cas.ROOT) for call in fake.calls))

    def test_fetched_snapshot_restores_both_forms_and_reowns_each_copy_once(self) -> None:
        added = hashlib.sha256(b"fictitious fetched object").hexdigest()
        value, listing = manifest_with_store({added: len(b"fictitious fetched object")})
        older_label = "20260901T110000Z"
        side = f"{backupset.STAGING}.fetch-{REMOTE_SNAPSHOT}"
        for version in (1, backupset.MANIFEST_VERSION):
            with self.subTest(version=version):
                current = replace(value, version=version)
                older = replace(current, label=older_label, started=current.started - timedelta(days=1),
                                finished=current.finished - timedelta(days=1), inventory={})
                fake = make_host(
                    source="target", manifest_value=current, store_listing=listing, store_live={},
                    names=(LOCAL_SET, older_label), side_names=(LOCAL_SET, older_label),
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n",
                )
                fake.files[os.path.join(backupset.set_dir(older_label), backupset.MANIFEST_NAME)] = older.to_json()
                fake.files[backupset.listing_path(backupset.set_dir(older_label), STORE_ROOT.name)] = listing
                code, output = self._run(fake, "target")
                self.assertEqual(code, 0, output)
                self.assertIn("fetch: ok", output)
                self.assertIn("swap: ok — installed the fetched staging directory", output)
                self.assertEqual(fake.store_live, {added: len(b"fictitious fetched object")})
                copies = (
                    (backupset.store_copy_dir(side, STORE_ROOT.name),) if current.store_in_staging else
                    tuple(backupset.store_copy(side, os.path.join(backupset.sets_dir(side), item.label), item, STORE_ROOT.name)
                          for item in (current, older))
                )
                reowns = [call for call in fake.calls if call[0][:4] == ("chown", "-R", "-h", "0:0")]
                self.assertEqual(reowns, [(("chown", "-R", "-h", "0:0", copy), None, None, None) for copy in copies])
                listing_paths = tuple(os.path.join(backupset.sets_dir(side), item.label, item.listings[STORE_ROOT.name].file)
                                      for item in (current, older))
                owners = [call for call in fake.calls if call[0][:3] == ("chown", "0:0", "--") and any(path in call[0] for path in listing_paths)]
                self.assertEqual(owners, [(("chown", "0:0", "--", *listing_paths), None, None, None)])
                modes = [call for call in fake.calls if call[0][:3] == ("chmod", "0644", "--") and any(path in call[0] for path in listing_paths)]
                self.assertEqual(len(modes), 1)
                self.assertEqual(tuple(path for path in modes[0][0][3:] if path in listing_paths), listing_paths)
                installed = backupset.store_copy(backupset.STAGING, backupset.set_dir(LOCAL_SET), current, STORE_ROOT.name)
                transfers = [call for call in fake.calls if "--files-from=-" in call[0]]
                self.assertEqual(len(transfers), 1)
                self.assertEqual(transfers[0][0][-2:], (installed + "/", os.fspath(cas.ROOT) + "/"))

    def test_damaged_missing_object_is_named_after_secrets_and_restore_succeeds(self) -> None:
        damaged = hashlib.sha256(b"intact source bytes").hexdigest()
        value, listing = manifest_with_store({damaged: len(b"intact source bytes")})
        fake = make_host(manifest_value=value, store_listing=listing, store_live={}, store_damaged=(damaged,))
        code, output = self._run(fake, "staging")

        self.assertEqual(code, 0, output)
        self.assertIn(f"{STORE_ROOT.name}: added 0 object(s), 1 left out", output)
        lines = output.splitlines()
        secrets_index = next(index for index, line in enumerate(lines) if line.startswith("Secrets:"))
        store_index = next(index for index, line in enumerate(lines) if line.startswith("Store:"))
        next_index = next(index for index, line in enumerate(lines) if line.startswith("Next:"))
        self.assertLess(secrets_index, store_index)
        self.assertLess(store_index, next_index)
        self.assertIn(damaged, lines[store_index])
        self.assertIn("each object's bytes", lines[store_index])
        self.assertFalse(any("--files-from=-" in call[0] for call in fake.calls))
        self.assertIn(
            f'"store": {{"{STORE_ROOT.name}": {{"added": 0, "left_out": 1}}}}',
            self._audit_text(fake),
        )

    def test_live_object_at_another_size_is_not_needed_or_replaced(self) -> None:
        held = hashlib.sha256(b"set copy bytes").hexdigest()
        value, listing = manifest_with_store({held: len(b"set copy bytes")})
        fake = make_host(manifest_value=value, store_listing=listing, store_live={held: 1})
        code, output = self._run(fake, "staging")

        self.assertEqual(code, 0, output)
        self.assertIn(f"{STORE_ROOT.name}: added 0 object(s), {backuproots.NOTHING_LEFT_OUT_DETAIL}", output)
        self.assertEqual(fake.store_live, {held: 1})
        self.assertFalse(any(call[0][:2] == ("sha256sum", "-c") and call[2] is not None and call[2].endswith("/files/" + STORE_ROOT.name) for call in fake.calls))
        self.assertFalse(any("--files-from=-" in call[0] for call in fake.calls))
        self.assertNotIn("Store:", output)

    def test_listing_pin_mismatch_refuses_before_stop(self) -> None:
        name = hashlib.sha256(b"set copy bytes").hexdigest()
        value, listing = manifest_with_store({name: len(b"set copy bytes")})
        pin = value.listings[STORE_ROOT.name]
        wrong = ("0" if pin.sha256[0] != "0" else "1") + pin.sha256[1:]
        value = replace(value, listings={STORE_ROOT.name: replace(pin, sha256=wrong)})
        for source in ("staging", "target"):
            with self.subTest(source=source):
                fake = make_host(
                    source=source, manifest_value=value, store_listing=listing, store_live={},
                    remote_listing=f"{REMOTE_SNAPSHOT}\t1\n" if source == "target" else "",
                )
                code, output = self._run(fake, source)
                self.assertEqual(code, 1)
                self.assertIn("verify: refuse", output)
                self.assertIn("does not match its manifest pin", output)
                self.assertIn("backup push --verify-all", output)
                self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))

    def _with_later(
        self, *, source: str, bad_later_pin: bool = False, bad_later_hash: bool = False
    ) -> tuple[FakeHost, str, str]:
        repaired = hashlib.sha256(b"whole object").hexdigest()
        later_only = hashlib.sha256(b"later object").hexdigest()
        selected, selected_listing = manifest_with_store({repaired: len(b"damaged object")})
        later, later_listing = manifest_with_store({
            repaired: len(b"whole object"), later_only: len(b"later object"),
        })
        upload_content = "whole uploaded file"
        upload_entry = replace(
            entry("uploads/data.bin", digest=hashlib.sha256(upload_content.encode()).hexdigest()),
            size=len(upload_content),
        )
        later = replace(later, inventory={**later.inventory, UPLOADS_ROOT.name: (upload_entry,)})
        later_finished = NOW - timedelta(minutes=10)
        later = replace(
            later, label=LATER_SET, started=later_finished - timedelta(minutes=10),
            finished=later_finished, archive_through=later_finished,
        )
        if bad_later_pin:
            pin = later.listings[STORE_ROOT.name]
            wrong = ("0" if pin.sha256[0] != "0" else "1") + pin.sha256[1:]
            later = replace(later, listings={STORE_ROOT.name: replace(pin, sha256=wrong)})
        fake = make_host(
            source=source, names=(LATER_SET, LOCAL_SET), side_names=(LATER_SET, LOCAL_SET),
            manifest_value=selected, store_listing=selected_listing, store_live={},
            store_damaged=(repaired,),
            remote_listing=f"{REMOTE_SNAPSHOT}\t1\n" if source == "target" else "",
            remote_record=backupset.PushRecord(REMOTE_SNAPSHOT, NOW, LATER_SET, NOW),
        )
        later_path = backupset.set_dir(LATER_SET)
        fake.files[os.path.join(later_path, backupset.MANIFEST_NAME)] = later.to_json()
        fake.files[backupset.listing_path(later_path, STORE_ROOT.name)] = later_listing
        later_copy = os.path.join(backupset.files_dir(later_path), UPLOADS_ROOT.name, upload_entry.path)
        fake.files[later_copy] = "damaged upload" if bad_later_hash else upload_content
        return fake, repaired, later_only

    def test_point_in_time_restore_uses_later_set_but_latest_restore_does_not(self) -> None:
        at = NOW - timedelta(minutes=30)
        for source in ("staging", "target"):
            with self.subTest(source=source):
                fake, repaired, later_only = self._with_later(source=source)
                code, output = self._run(fake, source, at=at)
                self.assertEqual(code, 0, output)
                self.assertIn(
                    f"{STORE_ROOT.name}: added 2 object(s), 0 left out (with {LATER_SET})", output,
                )
                self.assertIn(
                    f"{UPLOADS_ROOT.name}: added or rewrote 1 file(s) (with {LATER_SET})", output,
                )
                self.assertEqual(fake.store_live, {repaired: len(b"whole object"), later_only: len(b"later object")})
                self.assertEqual(
                    len([call for call in fake.calls if call[0][:3] == ("env", "LC_ALL=C", "join")]), 2,
                )
                self.assertIn(
                    f'"store": {{"{STORE_ROOT.name}": {{"added": 2, "left_out": 0}}}}',
                    self._audit_text(fake),
                )
                self.assertIn(f'"later_labels": ["{LATER_SET}"]', self._audit_text(fake))
                self.assertIn(f'"added": {{"{UPLOADS_ROOT.name}": 1}}', self._audit_text(fake))
                overlays = [
                    call for call in fake.calls if call[0][:3] == ("rsync", "-a", "--stats")
                ]
                self.assertEqual(len(overlays), 1)
                self.assertIn(f"/sets/{LATER_SET}/files/{UPLOADS_ROOT.name}/", overlays[0][0][-2])
                if source == "target":
                    self.assertIn("swap: ok — installed the fetched staging directory", output)

                latest, _, _ = self._with_later(source=source)
                code, output = self._run(latest, source)
                self.assertEqual(code, 0, output)
                self.assertNotIn(f"(with {LATER_SET})", output)
                self.assertNotIn(f"{UPLOADS_ROOT.name}: added or rewrote", output)
                self.assertFalse(any(call[0][:3] == ("rsync", "-a", "--stats") for call in latest.calls))
                self.assertIn('"later_labels": []', self._audit_text(latest))
                self.assertIn('"added": {}', self._audit_text(latest))
                self.assertEqual(
                    len([call for call in latest.calls if call[0][:3] == ("env", "LC_ALL=C", "join")]), 1,
                )

    def test_named_set_does_not_overlay_later_uploads(self) -> None:
        fake, _, _ = self._with_later(source="staging")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = restore.run_restore(
                argparse.Namespace(source="staging", at=None, set=LOCAL_SET), host=fake, now=NOW,
            )
        self.assertEqual(code, 0, output.getvalue())
        self.assertNotIn(f"{UPLOADS_ROOT.name}: added or rewrote", output.getvalue())
        self.assertFalse(any(call[0][:3] == ("rsync", "-a", "--stats") for call in fake.calls))
        self.assertIn('"later_labels": []', self._audit_text(fake))
        self.assertIn('"added": {}', self._audit_text(fake))

    def test_later_upload_hash_failure_refuses_before_stop(self) -> None:
        for source in ("staging", "target"):
            with self.subTest(source=source):
                fake, _, _ = self._with_later(source=source, bad_later_hash=True)
                code, output = self._run(fake, source, at=NOW - timedelta(minutes=30))
                self.assertEqual(code, 1)
                self.assertIn("verify: refuse", output)
                self.assertIn(
                    f"checksum verification failed for 1 path(s) in {UPLOADS_ROOT.name}", output,
                )
                self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))
                self.assertFalse(any(call[0][:3] == ("rsync", "-a", "--stats") for call in fake.calls))

    def test_later_listing_mismatch_refuses_before_stop(self) -> None:
        at = NOW - timedelta(minutes=30)
        for source in ("staging", "target"):
            with self.subTest(source=source):
                fake, _, _ = self._with_later(source=source, bad_later_pin=True)
                code, output = self._run(fake, source, at=at)
                self.assertEqual(code, 1)
                self.assertIn("verify: refuse", output)
                self.assertIn("does not match its manifest pin", output)
                self.assertIn("backup push --verify-all", output)
                self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))


class RestoreBySetLabel(RenderedRestoreCase):
    """``--set <label>`` selects the named set and restores it to its own archive boundary."""

    def run_restore(self, fake: FakeHost, **args: Any) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        namespace = argparse.Namespace(source="staging", at=None, set=None)
        for key, value in args.items():
            setattr(namespace, key, value)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(namespace, host=fake, now=NOW)
        return code, out.getvalue(), err.getvalue()

    def test_set_selects_the_named_set_and_targets_its_archive_boundary(self) -> None:
        fake = make_host()
        code, out, _ = self.run_restore(fake, set=LOCAL_SET)
        self.assertEqual(code, 0, out)
        boundary = manifest().archive_through
        self.assertIn(
            f"select: ok — source=staging; set={LOCAL_SET}; archive_through={boundary.isoformat()}",
            out,
        )
        self.assertNotIn("after the newest local archive boundary", out)
        restore_call = next(
            call[0]
            for call in fake.calls
            if call[0][0:2] == ("docker", "compose") and "pgbackrest" in call[0] and "restore" in call[0]
        )
        self.assertIn("--type=time", restore_call)
        self.assertIn(f"--target={backupset.pgbackrest_target(boundary)}", restore_call)
        self.assertIn("--target-action=promote", restore_call)
        # The set's own backup, never auto-selected: pgBackRest's rule (stop strictly
        # before the target, at second granularity) would skip it for the older one.
        self.assertIn(f"--set={manifest().pgbackrest_label}", restore_call)
        self.assertIn("--target-timeline=2", restore_call)
        self.assertIn("Next: sudo python3 -m gideon host provision", out)

    def test_target_restore_recovers_the_set_on_its_own_timeline_to_its_boundary(self) -> None:
        """--from target with no --at: the newest snapshot's set, its backup, its timeline, its boundary."""

        fake = make_host(source="target", remote_listing=f"{REMOTE_SNAPSHOT}\t1\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="target", at=None), host=fake, now=NOW)
        self.assertEqual(code, 0, out.getvalue())
        restore_call = next(
            call[0]
            for call in fake.calls
            if call[0][0:2] == ("docker", "compose") and "pgbackrest" in call[0] and "restore" in call[0]
        )
        self.assertIn(f"--set={manifest().pgbackrest_label}", restore_call)
        self.assertIn("--target-timeline=2", restore_call)
        self.assertIn("--type=time", restore_call)
        self.assertIn(f"--target={backupset.pgbackrest_target(manifest().archive_through)}", restore_call)
        self.assertIn("--target-action=promote", restore_call)

    def test_a_set_whose_backup_the_repository_lacks_refuses_at_postgres(self) -> None:
        fake = make_host(info_has_label=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = restore.run_restore(argparse.Namespace(source="staging", at=None), host=fake, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn(f"postgres: refuse — the repository holds no backup {manifest().pgbackrest_label}", out.getvalue())
        self.assertFalse(any("restore" in call[0] and "pgbackrest" in call[0] and "--delta" in call[0] for call in fake.calls))

    def test_set_is_refused_with_at_or_the_target_source(self) -> None:
        for args in ({"set": LOCAL_SET, "at": "2026-09-02T11:00Z"}, {"set": LOCAL_SET, "source": "target"}):
            with self.subTest(args=args):
                fake = make_host()
                code, out, err = self.run_restore(fake, **args)
                self.assertEqual(code, 1)
                self.assertIn(
                    "gideon restore: --set cannot be combined with --at or --from target. "
                    "Fix: Use --at for a point in time; use --from target for the off-box copy.",
                    err,
                )
                self.assertEqual(out, "")
                self.assertEqual(fake.calls, [])

    def test_an_unknown_or_partial_label_refuses_in_select_listing_the_complete_sets(self) -> None:
        for label in ("pre-missing", f"pre-v9.9.9{backupset.PARTIAL_SUFFIX}"):
            with self.subTest(label=label):
                fake = make_host(names=(LOCAL_SET, f"pre-v9.9.9{backupset.PARTIAL_SUFFIX}"))
                code, out, _ = self.run_restore(fake, set=label)
                self.assertEqual(code, 1)
                self.assertIn(
                    f"select: refuse — No complete backup set is named {label}; the complete sets are: {LOCAL_SET}. "
                    "Fix: Choose one of them, then retry sudo python3 -m gideon restore --from staging --set <label>.",
                    out,
                )
                self.assertFalse(any(call[0][-1] == "down" for call in fake.calls))


class ArchiveEndRestore(RenderedRestoreCase):
    """An archive-end restore replays the local archive without replacing the files or live indexes."""

    def _run(self, fake: FakeHost, *, archive_end: bool = True) -> tuple[int, str, str, list[float]]:
        out, err = io.StringIO(), io.StringIO()
        sleeps: list[float] = []
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = restore.run_restore(
                argparse.Namespace(source=fake.source, at=None, archive_end=archive_end),
                host=fake, now=NOW, sleep=sleeps.append,
            )
        return code, out.getvalue(), err.getvalue(), sleeps

    def _detail(self, fake: FakeHost) -> dict[str, Any]:
        sql = next(request for _, request, _, _ in fake.calls if request and "\\set v_kind 'restore'" in request)
        line = next(line for line in sql.splitlines() if line.startswith("\\set v_detail '"))
        return json.loads(line.removeprefix("\\set v_detail '").removesuffix("'"))

    def _row(self, output: str, name: str) -> str:
        return next(line for line in output.splitlines() if line.startswith(name + ":"))

    def _assert_not_stopped(self, fake: FakeHost) -> None:
        self.assertFalse(any(argv[-1] == "down" for argv, _, _, _ in fake.calls))
        self.assertFalse(any("pgbackrest" in argv and "restore" in argv for argv, _, _, _ in fake.calls))

    def test_target_source_refuses_before_select_or_ssh(self) -> None:
        fake = make_host(source="target")
        code, output, errors, _ = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("--archive-end", errors)
        self.assertIn("cannot use --from target", errors)
        self.assertIn(report.command("restore --from staging --archive-end"), errors)
        self.assertFalse(any(argv[0] == "ssh" for argv, _, _, _ in fake.calls))
        self._assert_not_stopped(fake)

    def test_running_postgres_refuses_with_the_time_target_and_stop_fix(self) -> None:
        fake = make_host(running=True)
        code, output, errors, _ = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "")
        row = self._row(output, "pre-restore")
        self.assertIn("pre-restore: refuse", row)
        self.assertIn("Postgres answers", row)
        self.assertIn(report.command("restore --from staging --at <instant>"), row)
        self.assertIn("or omit --at", row)
        self.assertIn("docker compose -f /etc/gideon/rendered/compose.yaml down", row)
        self._assert_not_stopped(fake)

    def test_partial_stack_keeps_its_refusal(self) -> None:
        fake = make_host(running=True, partial=True)
        code, output, errors, _ = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "")
        self.assertIn("pre-restore: refuse — the stack is partially running", output)
        self._assert_not_stopped(fake)

    def test_unreachable_latest_timeline_names_the_newest_boundary(self) -> None:
        fake = make_host(history_files={"00000003.history": "1 0/1000000 first fork\n2 0/6000000 second fork\n"})
        code, output, errors, _ = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "")
        row = self._row(output, "select")
        self.assertIn("select: refuse", row)
        self.assertIn("latest timeline 3", row)
        self.assertIn(f"timeline={fake.backup_timeline}", row)
        self.assertIn(fake.manifest_value.archive_through.isoformat(), row)
        self.assertIn(report.command("restore --from staging"), row)
        self.assertIn(report.command("backup run --full"), row)
        self._assert_not_stopped(fake)

    def test_empty_archive_refuses_with_the_backup_fix(self) -> None:
        fake = make_host(archive_max=None)
        code, output, errors, _ = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "")
        row = self._row(output, "select")
        self.assertIn("select: refuse — the local archive has no archived WAL segment", row)
        self.assertIn(report.command("backup run"), row)
        self._assert_not_stopped(fake)

    def test_stopped_restore_keeps_files_and_indexes_levels_and_audits_the_reach(self) -> None:
        live = generation("live")
        built = generation("built", label="corpus-2099-02-03")
        value, listing = manifest_with_store({"a" * 64: 7})
        value = replace(value, inventory={**value.inventory, **{root.name: () for root in DERIVED_ROOTS}})
        fake = make_host(
            manifest_value=value, store_listing=listing, store_live={"b" * 64: 9},
            qdrant_files=(f"{live.gen_id}/present.snapshot",),
            generations=(asdict(live), asdict(built)),
        )
        fake.index_counts["qdrant"][built.gen_id] = built.counts["dense_total"] - 1
        before_files, before_store = dict(fake.files), dict(fake.store_live or {})
        code, output, errors, sleeps = self._run(fake)
        self.assertEqual(code, 0, output + errors)
        self.assertEqual(errors, "")
        self.assertEqual(sleeps, [])
        self.assertIn("pre-restore: ok — skipped (stack not running)", output)
        self.assertIn("files: ok — skipped (every root left in place)", output)
        self.assertEqual(fake.files, before_files)
        self.assertEqual(fake.store_live, before_store)
        self.assertFalse(any(argv[0] in {"rsync", "chown"} for argv, _, _, _ in fake.calls))
        restore_argv = next(argv for argv, _, _, _ in fake.calls if "pgbackrest" in argv and "restore" in argv)
        self.assertEqual(restore_argv[restore_argv.index("restore") + 1:], (
            "--delta", f"--set={value.pgbackrest_label}", "--type=default", "--target-timeline=latest",
        ))
        self.assertFalse(any(
            argv[0] == "sha256sum" and backupset.TARBALL_NAME in " ".join(argv)
            for argv, _, _, _ in fake.calls
        ))
        checks = [call for call in fake.calls if call[0][:2] == ("sha256sum", "-c")]
        repository = next(root for root in backupset.inventory_roots(value.checkout) if root.kind is backupset.RootKind.REPOSITORY)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][2], repository.source)
        self.assertNotIn("backup.info", checks[0][1] or "")
        self.assertNotIn("archive.info", checks[0][1] or "")
        for _, request, _, _ in fake.calls:
            self.assertNotIn("/snapshots/recover", request or "")
            self.assertNotIn("/_snapshot/", request or "")
        self.assertEqual(fake.generations[1]["state"], "building")
        for targets in fake.index_aliases.values():
            self.assertEqual(targets[live.collection], (live.gen_id,))
        detail = self._detail(fake)
        self.assertEqual({key: detail[key] for key in (
            "reached", "reached_lsn", "reached_segment", "archive_end", "archive_end_reached",
        )}, {
            "reached": fake.replay.instant.isoformat() if fake.replay.instant else None,
            "reached_lsn": fake.replay.lsn, "reached_segment": fake.replay.segment,
            "archive_end": fake.archive_max, "archive_end_reached": True,
        })
        self.assertEqual(detail["indexes"], {})
        for name in ("stores", "Reached"):
            row = self._row(output, name)
            self.assertIn(fake.replay.lsn or "", row)
            self.assertIn(fake.replay.segment, row)
            self.assertIn(fake.archive_max or "", row)
            self.assertIn("archive end reached", row)
        replay_read = next(i for i, (_, sql, _, _) in enumerate(fake.calls) if sql and "pg_last_xact_replay_timestamp()" in sql)
        # A replay ending on a WAL switch stops on the next segment's first byte.
        self.assertIn("pg_walfile_name(pg_last_wal_replay_lsn() - 1)", fake.calls[replay_read][1] or "")
        demotion = next(i for i, (_, sql, _, _) in enumerate(fake.calls) if sql and "DO $demotion$" in sql)
        self.assertLess(replay_read, demotion)

    def test_short_past_null_instant_and_rollover_replays_are_reported(self) -> None:
        for label, replay, maximum, expected, reached in (
            ("short", Replay(segment="000000030000000000000007"), "00000002000000000000000A", "short by 3 segment(s)", False),
            ("past", Replay(segment="00000003000000000000000B"), "00000002000000000000000A", "past the archive's newest segment", True),
            ("no commit", Replay(instant=None), "00000002000000000000000A", "no commit replayed", True),
            ("rollover", Replay(lsn="1/1", segment="000000030000000100000000"), "0000000200000000000000FF", "past the archive's newest segment", True),
            ("rollover short", Replay(lsn="0/FF000000", segment="0000000300000000000000FF"), "000000020000000100000000", "short by 1 segment(s)", False),
        ):
            with self.subTest(label=label):
                fake = make_host(replay=replay, archive_max=maximum)
                code, output, errors, _ = self._run(fake)
                self.assertEqual(code, 0, output + errors)
                for name in ("stores", "Reached"):
                    row = self._row(output, name)
                    self.assertIn(expected, row)
                    if not reached:
                        self.assertIn("docs/runbooks/backup-restore.md §4", row)
                    else:
                        self.assertNotIn("short by", row)
                detail = self._detail(fake)
                self.assertEqual(detail["archive_end_reached"], reached)
                self.assertEqual(detail["reached"], replay.instant.isoformat() if replay.instant else None)

    def test_recovery_polls_wait_before_reading_the_reach(self) -> None:
        fake = make_host(replay=Replay(recovery_polls=3))
        code, output, errors, sleeps = self._run(fake)
        self.assertEqual(code, 0, output + errors)
        self.assertEqual(len(sleeps), fake.replay.recovery_polls)
        self.assertTrue(all(pause > 0 for pause in sleeps))
        self.assertEqual(fake.recovery_polls_remaining, 0)
        self.assertTrue(self._detail(fake)["archive_end_reached"])

    def test_recovery_bound_refuses_before_level_or_audit(self) -> None:
        fake = make_host(replay=Replay(recovery_polls=int(stores.REPLAY_WAIT_SECONDS) + 1))
        code, output, errors, sleeps = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "")
        self.assertEqual(sum(sleeps), stores.REPLAY_WAIT_SECONDS)
        row = self._row(output, "stores")
        self.assertIn("stores: refuse", row)
        self.assertIn("still in recovery", row)
        self.assertIn("logs postgres", row)
        self.assertEqual(fake.generation_statements, [])
        self.assertNotIn("\\set v_kind 'restore'", "\n".join(sql or "" for _, sql, _, _ in fake.calls))
        self.assertNotIn("Reached:", output)
        self.assertTrue(fake.running)
        problem = stores.replay_reach(fake, RENDERED, sleep=sleeps.append, wait_seconds=0)
        self.assertIsInstance(problem, report.Timeout)
        assert isinstance(problem, report.Timeout)
        self.assertIn("still in recovery", problem.problem)
        self.assertEqual(problem.fix, stack.logs_fix(RENDERED, "postgres"))

    def test_backup_before_a_timeline_switch_reaches_the_latest_archive(self) -> None:
        fake = make_host(
            history_files={"00000003.history": "1 0/4000000 first fork\n2 0/8000000 second fork\n"},
            archive_max="00000003000000000000000A", replay=Replay(segment="00000004000000000000000A"),
        )
        code, output, errors, _ = self._run(fake)
        self.assertEqual(code, 0, output + errors)
        self.assertIn(f"timeline={fake.backup_timeline}; latest_timeline=3", self._row(output, "select"))

    def test_unknown_backup_stop_lsn_is_named_and_not_selected(self) -> None:
        fake = make_host(lsn_stop=None)
        code, output, errors, _ = self._run(fake)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "")
        self.assertIn("select: refuse", output)
        self.assertIn(f"backup {fake.manifest_value.pgbackrest_label} records no stop LSN", output)
        self._assert_not_stopped(fake)

    def test_null_position_and_malformed_reaches_refuse_before_level(self) -> None:
        for replay, expected in (
            (Replay(lsn=None), "no replay position"),
            (Replay(segment="not-a-segment"), "malformed replay reach"),
            (Replay(segment_size=0), "malformed replay reach"),
        ):
            with self.subTest(replay=replay):
                fake = make_host(replay=replay)
                code, output, errors, _ = self._run(fake)
                self.assertEqual(code, 1)
                self.assertEqual(errors, "")
                row = self._row(output, "stores")
                self.assertIn("stores: refuse", row)
                self.assertIn(expected, row)
                self.assertIn("logs postgres", row)
                self.assertEqual(fake.generation_statements, [])
                self.assertNotIn("Reached:", output)

    def test_set_with_a_missing_backup_is_passed_over_and_named(self) -> None:
        older = manifest()
        newer = replace(older, label=LATER_SET, finished=NOW - timedelta(minutes=10), pgbackrest_label="missing-backup")
        fake = make_host(names=(LATER_SET, LOCAL_SET), manifest_value=older)
        fake.files[os.path.join(backupset.set_dir(newer.label), backupset.MANIFEST_NAME)] = newer.to_json()
        code, output, errors, _ = self._run(fake)
        self.assertEqual(code, 0, output + errors)
        row = self._row(output, "select")
        self.assertIn(f"set={older.label}; backup={older.pgbackrest_label}", row)
        self.assertIn(f"set {newer.label}: the repository holds no backup {newer.pgbackrest_label}", row)

    def test_rotated_secrets_use_the_live_files_and_bare_restore_keeps_its_line(self) -> None:
        for archive_end in (True, False):
            with self.subTest(archive_end=archive_end):
                fake = make_host(secrets_present=False)
                code, output, errors, _ = self._run(fake, archive_end=archive_end)
                self.assertEqual(code, 0, output + errors)
                line = self._row(output, "Secrets")
                if archive_end:
                    self.assertIn("the secrets on disk are the live box's", line)
                    self.assertIn("carries every rotation up to the archive end", line)
                    self.assertIn("nothing is decrypted", line)
                    self.assertNotIn("age -d", line)
                    lines = output.splitlines()
                    self.assertTrue(lines[lines.index(line) + 1].startswith("Reached:"))
                else:
                    tarball = os.path.join(backupset.set_dir(LOCAL_SET), backupset.TARBALL_NAME)
                    self.assertEqual(line, "Secrets: the secrets on disk are not the set's (a rebuilt box): decrypt "
                                     "the tarball with the office's age identity: age -d -i "
                                     f"<identity file kept off-box> {tarball} | tar -x -C /etc/gideon")
                    self.assertNotIn("Reached:", output)
                    for key in ("reached", "reached_lsn", "reached_segment", "archive_end", "archive_end_reached"):
                        self.assertNotIn(key, self._detail(fake))
