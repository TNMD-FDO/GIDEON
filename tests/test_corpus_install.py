"""Corpus install stages over fictitious committed lockfiles."""

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from gideon.host import backuplock, courts, fetch, report, stack, staging, worker
from gideon.host.corpus import install, snapshots
from gideon.host.corpus.lockfile import (
    IndexDocument,
    Lockfile,
    SidecarEntry,
    SourcePin,
    load_lockfile,
    render_lockfile,
    render_sidecar,
)
from gideon.host.corpus.sources import SourceDefinition, SourceResolution, data_file
from gideon.host.egress import load_egress_allowlist
from gideon.host.render import worker as worker_identity
from gideon.host.sysio import Command, PathLike, RealHost

ROOT = Path(__file__).resolve().parent.parent
RENDERED = "/rendered"
NOW = datetime(2099, 1, 5, 4, 5, 6, tzinfo=UTC)
FILE_PATH = "objects/fictitious.txt"


def _whole_record(data: bytes, url: str, job: int) -> dict[str, object]:
    parsed = urlsplit(url)
    return {
        "schema": 1, "state": "whole", "form": worker_identity.KEPT_FORM,
        "host": parsed.hostname, "path": parsed.path,
        "etag": None, "last_modified": None,
        "total": len(data), "durable": len(data), "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "fetched_at": NOW.isoformat(), "job": job, "seconds": 0.1, "resumes": 0,
    }


class FakeHost(RealHost):
    """Real temporary files with recorded worker, database, hash, and lock calls."""

    def __init__(self) -> None:
        super().__init__()
        self.euid = 0
        self.record_rows: dict[str, dict[str, Any]] = {}
        self.snapshot_rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.last_payload: dict[str, Any] | None = None
        self.record_writes = 0
        self.supersede_on_write = False
        self.lock_holder: str | None = None
        self.lock_releases = 0
        self.calls: list[list[str]] = []
        self.snapshots = Path("/unused")
        self.work = Path("/unused-work")
        self.current_label = ""
        self.served_bytes: dict[str, bytes] = {}
        self.deferred_urls: list[str] = []
        self.job_destinations: dict[int, str] = {}
        self.stage_jobs: dict[int, dict[str, Any]] = {}
        self.stage_deferred: list[dict[str, Any]] = []
        self.stage_failure: str | None = None
        self.interrupt_stage_job = False
        self.caselaw_jobs: dict[int, dict[str, Any]] = {}
        self.caselaw_deferred: list[dict[str, Any]] = []
        self.caselaw_failure: str | None = None
        self.caselaw_counts: dict[str, dict[str, object]] = {}
        self.caselaw_section_counts: dict[str, dict[str, object]] = {}
        self.caselaw_anchor_counts: dict[str, dict[str, object]] = {}
        self.caselaw_citation_counts: dict[str, dict[str, object]] = {}
        self.caselaw_treatment_counts: dict[str, dict[str, object]] = {}
        self.interrupt_caselaw_job = False
        self.caselaw_events: list[tuple[str, str]] = []
        self.job_polls: dict[int, int] = {}
        self.next_job = 100
        self.unreadable_root = False
        self.interrupt_rmtree = False
        self.interrupt_job = False
        self.removed_dirs: list[Path] = []

    def listdir(self, path: PathLike) -> list[str]:
        if self.unreadable_root and Path(path) == self.snapshots:
            raise OSError("unreadable snapshots root")
        return super().listdir(path)

    def rmtree(self, path: PathLike) -> None:
        if self.interrupt_rmtree:
            raise KeyboardInterrupt
        self.removed_dirs.append(Path(path))
        super().rmtree(path)

    @staticmethod
    def _bound(sql: str, name: str) -> str:
        match = re.search(rf"\\set {name} '([^']*)'", sql)
        assert match is not None, sql
        return match.group(1)

    def _write_fetch(self, job: int, destination: str, url: str) -> None:
        data = self.served_bytes[url]
        path = self.snapshots / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        (self.snapshots / f"{destination}{worker_identity.RECORD_SUFFIX}").write_text(
            json.dumps(_whole_record(data, url, job))
        )

    def stage_record(self, job: int, args: dict[str, Any]) -> dict[str, Any]:
        inputs = {
            table: {**item, "size": (
                self.snapshots / args["snapshot"] / item["path"]
            ).stat().st_size}
            for table, item in args["inputs"].items()
        }
        return {
            "schema": 1, "label": args["label"],
            "source": args["snapshot"][:-staging.DATE_SUFFIX_LENGTH],
            "snapshot": args["snapshot"], "courts": args["courts"],
            "inputs": inputs,
            "records": dict.fromkeys(worker_identity.STAGE_TABLES, 3),
            "counts": {
                court: dict.fromkeys(worker_identity.STAGE_TABLES[1:], index + 1)
                for index, court in enumerate(args["courts"])
            },
            "job": job, "seconds": 7.6, "staged_at": NOW.isoformat(),
        }

    def _write_stage(self, job: int, args: dict[str, Any]) -> None:
        directory = staging.work_directory(
            args["label"], args["snapshot"][:-staging.DATE_SUFFIX_LENGTH],
            work_root=self.work,
        )
        directory.parent.mkdir(parents=True, exist_ok=True)
        if self.stage_failure is not None:
            failure = {
                "schema": 1, "job": job, "reason": self.stage_failure,
                "table": "opinions" if self.stage_failure == "malformed" else None,
                "court": None, "error": None, "at": NOW.isoformat(),
            }
            (directory.with_name(
                directory.name + "." + worker_identity.STAGE_FAILURE_NAME
            )).write_text(json.dumps(failure))
        else:
            directory.mkdir()
            (directory / worker_identity.STAGE_RECORD_NAME).write_text(
                json.dumps(self.stage_record(job, args))
            )

    def geteuid(self) -> int:
        return self.euid

    def mkdir(
        self, path: PathLike, *, mode: int = 0o755,
        parents: bool = False, exist_ok: bool = False,
    ) -> None:
        if str(path) != backuplock.LOCK_DIR:
            super().mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    def take_lock(self, path: PathLike, record: str) -> str | None:
        assert str(path) == backuplock.CORPUS_LOCK.path
        if self.lock_holder is not None:
            return self.lock_holder
        self.lock_holder = record
        return None

    def release_lock(self, path: PathLike) -> None:
        assert str(path) == backuplock.CORPUS_LOCK.path
        self.lock_holder = None
        self.lock_releases += 1

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        if str(path) == f"{RENDERED}/compose.yaml":
            return f"services:\n  {worker_identity.WORKER_SERVICE_NAME}: {{}}\n"
        return super().read_text(path, encoding=encoding)

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        command = list(argv)
        self.calls.append(command)
        if command == stack.compose_argv(RENDERED, "ps", "--all", "--format", "json"):
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "Service": worker_identity.WORKER_SERVICE_NAME,
                "State": "running", "Health": "healthy",
            }), "")
        if command == worker.psql_argv(RENDERED):
            assert input is not None
            if "procrastinate_defer_jobs_v1" in input:
                args = json.loads(self._bound(input, "v_args"))
                job = self.next_job
                self.next_job += 1
                self.job_polls[job] = 0
                if self._bound(input, "v_task") == worker_identity.CASELAW_TASK:
                    self.caselaw_jobs[job] = args
                    self.caselaw_deferred.append(args)
                    self.caselaw_events.append(("defer", args["court"]))
                    if self.caselaw_failure is not None:
                        directory = staging.work_directory(
                            args["label"], args["snapshot"][:-staging.DATE_SUFFIX_LENGTH],
                            work_root=self.work,
                        )
                        (directory / f"{args['court']}.{worker_identity.CASELAW_FAILURE_NAME}").write_text(
                            json.dumps({
                                "schema": 1, "job": job, "court": args["court"],
                                "reason": self.caselaw_failure, "table": None,
                                "error": None, "at": NOW.isoformat(),
                            })
                        )
                elif "snapshot" in args:
                    self.stage_jobs[job] = args
                    self.stage_deferred.append(args)
                    self._write_stage(job, args)
                else:
                    destination = args["destination"]
                    url = args["url"]
                    self.deferred_urls.append(url)
                    self.job_destinations[job] = destination
                    self._write_fetch(job, destination, url)
                return subprocess.CompletedProcess(command, 0, f"{job}\n", "")
            if "FROM procrastinate_jobs" in input:
                job = int(self._bound(input, "v_job_id"))
                if self.interrupt_job and job in self.job_destinations:
                    raise KeyboardInterrupt
                if self.interrupt_stage_job and job in self.stage_jobs:
                    raise KeyboardInterrupt
                if self.interrupt_caselaw_job and job in self.caselaw_jobs:
                    raise KeyboardInterrupt
                assert job in self.job_destinations or job in self.stage_jobs or job in self.caselaw_jobs
                if job in self.caselaw_jobs:
                    self.caselaw_events.append(("read", self.caselaw_jobs[job]["court"]))
                polls = self.job_polls[job]
                self.job_polls[job] += 1
                status = (
                    "doing" if polls == 0 else
                    "failed" if (job in self.stage_jobs and self.stage_failure is not None)
                    or (job in self.caselaw_jobs and self.caselaw_failure is not None)
                    else "succeeded"
                )
                return subprocess.CompletedProcess(command, 0, f"{job}|{status}|1\n", "")
            if "SELECT d.doc_id, COALESCE(d.text_source, '') AS text_source, d.anchored_at" in input:
                court = self._bound(input, "v_court")
                if court in self.caselaw_anchor_counts:
                    counts = self.caselaw_anchor_counts[court]
                else:
                    by_status = self.caselaw_counts.get(court, {}).get("by_status")
                    if isinstance(by_status, dict):
                        ready = by_status.get("ready", 0)
                    else:
                        source = self._bound(input, "v_source")
                        directory = staging.work_directory(
                            self.current_label, source, work_root=self.work,
                        )
                        stage_record = json.loads(
                            (directory / worker_identity.STAGE_RECORD_NAME).read_text()
                        )
                        ready = stage_record["counts"][court]["opinions"]
                    counts = {
                        "ready": ready, "anchored": ready,
                        "by_text_source": {
                            "xml_harvard": {
                                "documents": ready, "with_anchors": ready,
                                "chars": 10 * ready, "anchored_chars": 10 * ready,
                            },
                        } if ready else {},
                    }
                return subprocess.CompletedProcess(command, 0, json.dumps(counts) + "\n", "")
            if "FROM public.citation_signals AS s" in input:
                court = self._bound(input, "v_court")
                self._bound(input, "v_pattern_set")
                if court in self.caselaw_treatment_counts:
                    counts = self.caselaw_treatment_counts[court]
                else:
                    by_status = self.caselaw_counts.get(court, {}).get("by_status")
                    if isinstance(by_status, dict):
                        ready = by_status.get("ready", 0)
                    else:
                        source = self._bound(input, "v_source")
                        directory = staging.work_directory(
                            self.current_label, source, work_root=self.work,
                        )
                        stage_record = json.loads(
                            (directory / worker_identity.STAGE_RECORD_NAME).read_text()
                        )
                        ready = stage_record["counts"][court]["opinions"]
                    counts = {
                        "ready": ready, "treated": ready, "signalled": 0,
                        "by_signal": {}, "by_outcome": {}, "by_qualifier": {},
                        "by_effective_section": {},
                    }
                return subprocess.CompletedProcess(command, 0, json.dumps(counts) + "\n", "")
            if "FROM public.citations AS c" in input:
                court = self._bound(input, "v_court")
                if court in self.caselaw_citation_counts:
                    counts = self.caselaw_citation_counts[court]
                else:
                    by_status = self.caselaw_counts.get(court, {}).get("by_status")
                    if isinstance(by_status, dict):
                        ready = by_status.get("ready", 0)
                    else:
                        source = self._bound(input, "v_source")
                        directory = staging.work_directory(
                            self.current_label, source, work_root=self.work,
                        )
                        stage_record = json.loads(
                            (directory / worker_identity.STAGE_RECORD_NAME).read_text()
                        )
                        ready = stage_record["counts"][court]["opinions"]
                    counts = {
                        "ready": ready, "cited": ready, "edges": 0,
                        "by_type": {}, "by_form": {},
                        "case_rows": 0, "case_resolved": 0,
                    }
                return subprocess.CompletedProcess(command, 0, json.dumps(counts) + "\n", "")
            if "WITH ready_documents AS" in input:
                court = self._bound(input, "v_court")
                if court in self.caselaw_section_counts:
                    counts = self.caselaw_section_counts[court]
                else:
                    by_status = self.caselaw_counts.get(court, {}).get("by_status")
                    if isinstance(by_status, dict):
                        ready = by_status.get("ready", 0)
                    else:
                        source = self._bound(input, "v_source")
                        directory = staging.work_directory(
                            self.current_label, source, work_root=self.work,
                        )
                        stage_record = json.loads(
                            (directory / worker_identity.STAGE_RECORD_NAME).read_text()
                        )
                        ready = stage_record["counts"][court]["opinions"]
                    counts = {
                        "ready": ready, "sectioned": ready,
                        "sections_by_type": {"majority": ready} if ready else {},
                        "chars_by_type": {"majority": 10 * ready} if ready else {},
                    }
                return subprocess.CompletedProcess(command, 0, json.dumps(counts) + "\n", "")
            if "JOIN public.opinions" in input:
                court = self._bound(input, "v_court")
                self.caselaw_events.append(("counts", court))
                if court in self.caselaw_counts:
                    counts = self.caselaw_counts[court]
                else:
                    source = self._bound(input, "v_source")
                    directory = staging.work_directory(self.current_label, source, work_root=self.work)
                    stage_record = json.loads((directory / worker_identity.STAGE_RECORD_NAME).read_text())
                    count = stage_record["counts"][court]["opinions"]
                    counts = {
                        "opinions": count, "by_status": {"ready": count},
                        "by_text_source": {"xml_harvard": count},
                        "by_precedential": {"published": count},
                        "by_failure_reason": {},
                    }
                return subprocess.CompletedProcess(command, 0, json.dumps(counts) + "\n", "")
            if "DO $cut$" in input:
                self.record_writes += 1
                payload: dict[str, Any] = json.loads(self._bound(input, "v_payload"))
                candidate = {key: payload[key] for key in (
                    "label", "schema", "pipeline", "cut_at", "reason", "base",
                )}
                candidate["sources"] = [
                    {"source": pin["source"], "snapshot_date": pin["snapshot_date"]}
                    for pin in payload["sources"]
                ]
                held = self.record_rows.get(payload["label"])
                if self.supersede_on_write:
                    assert held is not None
                    held["state"] = "superseded"
                    self.supersede_on_write = False
                if held is not None and any(
                    held[key] != value for key, value in candidate.items()
                ):
                    return subprocess.CompletedProcess(
                        command, 3, "",
                        f"ERROR: CORPUS_LOCKFILE_CONFLICT:{payload['label']}\n",
                    )
                pin = payload["sources"][0]
                snapshot_key = (pin["source"], pin["snapshot_date"])
                snapshot = self.snapshot_rows.get(snapshot_key)
                if snapshot is not None and any(
                    snapshot[key] != pin[key] for key in ("base_url", "sidecar_sha256")
                ):
                    return subprocess.CompletedProcess(
                        command, 3, "",
                        f"ERROR: CORPUS_SNAPSHOT_CONFLICT:{pin['source']}:{pin['snapshot_date']}\n",
                    )
                if (payload.get("install") is True and held is not None
                    and held["state"] == "superseded"):
                    return subprocess.CompletedProcess(
                        command, 3, "",
                        f"ERROR: CORPUS_LOCKFILE_SUPERSEDED:{payload['label']}\n",
                    )
                if held is None:
                    held = {**candidate, "installed_at": None, "state": "cut"}
                    self.record_rows[payload["label"]] = held
                if payload.get("install") is True and held["state"] == "cut":
                    held["state"] = "installing"
                if snapshot is None:
                    self.snapshot_rows[snapshot_key] = {
                        **pin, "verified_at": payload["verified_at"],
                    }
                else:
                    snapshot.update(
                        mirror_url=pin["mirror_url"], verified_at=payload["verified_at"],
                    )
                self.last_payload = payload
                return subprocess.CompletedProcess(command, 0, "", "")
            assert "jsonb_build_object" in input
            if "ORDER BY c.label" in input:
                lines = "".join(
                    json.dumps(row) + "\n" for _, row in sorted(self.record_rows.items())
                )
                return subprocess.CompletedProcess(command, 0, lines, "")
            label = self._bound(input, "v_label")
            if label not in self.record_rows:
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.record_rows[label]) + "\n", "",
            )
        if command[0] == "sha256sum":
            path = Path(command[1])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return subprocess.CompletedProcess(command, 0, f"{digest}  {path}\n", "")
        raise AssertionError(f"unexpected command: {command}")


class Install(unittest.TestCase):
    """The selected lockfile is checked before its files are hashed."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.checkout = root / "checkout"
        self.snapshots = root / "snapshots"
        self.work = root / "work"
        self.work.mkdir()
        (self.checkout / "config").mkdir(parents=True)
        shutil.copy2(ROOT / "courts.yaml", self.checkout / "courts.yaml")
        shutil.copy2(ROOT / "config/egress.yaml", self.checkout / "config/egress.yaml")
        allowlist = load_egress_allowlist(self.checkout / "config/egress.yaml")
        assert allowlist.allowlist is not None
        group = allowlist.allowlist.group("corpus")
        assert group is not None
        base_url = f"https://{group.hosts[0].host}/archive/"
        self.mirror_url = f"https://{group.hosts[0].host}/mirror/"
        self.source = SourceDefinition(
            "example", base_url, False, (), lambda: (),
            lambda _documents: SourceResolution("2099-01-02", ()),
        )
        self.labels: tuple[str, ...] = ("corpus-2099-01-03", "corpus-2099-01-04")
        for label, date, data in (
            (self.labels[0], "2099-01-02", b"fictitious first snapshot"),
            (self.labels[1], "2099-01-03", b"fictitious second snapshot"),
        ):
            self._write_snapshot(label, date, data)
        self.host = FakeHost()
        self.host.snapshots = self.snapshots
        self.host.work = self.work
        self.destination = fetch.snapshot_destination("example", "2099-01-02", FILE_PATH)
        self.file = self.snapshots / self.destination
        self.record_file = self.snapshots / f"{self.destination}{worker_identity.RECORD_SUFFIX}"
        self.pinned_bytes = self.file.read_bytes()

    def _write_snapshot(self, label: str, date: str, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        entry = SidecarEntry(FILE_PATH, digest, len(data))
        sidecar = render_sidecar((entry,))
        index_bytes = f"<fictitious-snapshot date='{date}'/>\n".encode()
        index = IndexDocument(
            "listing.xml", f"{self.source.base_url}listing.xml",
            hashlib.sha256(index_bytes).hexdigest(), len(index_bytes),
        )
        pin = SourcePin(
            date, self.source.base_url, None,
            hashlib.sha256(sidecar.encode()).hexdigest(), 1, len(data), (index,), (entry,), None,
        )
        lockfile = Lockfile(1, label, "0.0.0", "2099-01-03T04:05:06Z", "tranche", {"example": pin})
        directory = self.checkout / "corpus/lockfiles"
        (directory / label).mkdir(parents=True)
        (directory / label / "example.sha256").write_text(sidecar)
        (directory / label / "example.listing.xml").write_bytes(index_bytes)
        (directory / f"{label}.yaml").write_text(render_lockfile(lockfile))

        destination = fetch.snapshot_destination("example", date, FILE_PATH)
        path = self.snapshots / destination
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        url = f"{self.source.base_url}{FILE_PATH}"
        (self.snapshots / f"{destination}{worker_identity.RECORD_SUFFIX}").write_text(
            json.dumps(_whole_record(data, url, 1))
        )

    def _remove_snapshot(self) -> None:
        self.file.unlink()
        self.record_file.unlink()

    def _set_mirror(self) -> None:
        path = self.checkout / "corpus/lockfiles" / f"{self.labels[0]}.yaml"
        result = load_lockfile(path, known_sources={"example": self.source.carries_courts})
        assert result.lockfile is not None
        lockfile = result.lockfile
        pin = lockfile.sources["example"]
        path.write_text(render_lockfile(replace(
            lockfile, sources={"example": replace(pin, mirror_url=self.mirror_url)},
        )))

    def _hold_record(self, state: str, label: str | None = None) -> None:
        path = self.checkout / "corpus/lockfiles" / f"{label or self.labels[0]}.yaml"
        loaded = load_lockfile(path, known_sources={"example": self.source.carries_courts})
        assert loaded.lockfile is not None
        lockfile = loaded.lockfile
        pin = lockfile.sources["example"]
        self.host.record_rows[lockfile.label] = {
            "label": lockfile.label, "schema": lockfile.schema,
            "pipeline": lockfile.pipeline, "cut_at": lockfile.cut_at,
            "reason": lockfile.reason, "base": lockfile.base,
            "installed_at": None, "state": state,
            "sources": [{"source": "example", "snapshot_date": pin.snapshot_date}],
        }
        self.host.snapshot_rows[("example", pin.snapshot_date)] = {
            "source": "example", "snapshot_date": pin.snapshot_date,
            "base_url": pin.base_url, "mirror_url": pin.mirror_url,
            "sidecar_sha256": pin.sidecar_sha256,
            "fetched_at": NOW.isoformat(), "verified_at": NOW.isoformat(),
        }

    def _add_third_lockfile(self) -> str:
        label = "corpus-2099-01-05"
        self._write_snapshot(label, "2099-01-04", b"fictitious third snapshot")
        self.labels += (label,)
        return label

    def _enable_stage(self, *, missing: str | None = None) -> None:
        """Give each committed fixture a court pin and dated stage inputs."""

        for label in self.labels:
            lock_path = self.checkout / "corpus/lockfiles" / f"{label}.yaml"
            loaded = load_lockfile(lock_path, known_sources={"example": False})
            assert loaded.lockfile is not None
            lockfile = loaded.lockfile
            pin = lockfile.sources["example"]
            entries = list(pin.entries)
            for table in worker_identity.STAGE_TABLES:
                if label == self.labels[0] and table == missing:
                    continue
                name = data_file(table, pin.snapshot_date)
                body = f"fictitious {table} rows for {label}\n".encode()
                entry = SidecarEntry(name, hashlib.sha256(body).hexdigest(), len(body))
                entries.append(entry)
                destination = fetch.snapshot_destination("example", pin.snapshot_date, name)
                path = self.snapshots / destination
                path.write_bytes(body)
                url = f"{self.source.base_url}{name}"
                (path.with_name(path.name + worker_identity.RECORD_SUFFIX)).write_text(
                    json.dumps(_whole_record(body, url, 1))
                )
            sidecar = render_sidecar(entries)
            (lock_path.parent / label / "example.sha256").write_text(sidecar)
            updated = replace(
                pin, sidecar_sha256=hashlib.sha256(sidecar.encode()).hexdigest(),
                files=len(entries), bytes=sum(entry.size for entry in entries),
                entries=tuple(sorted(entries, key=lambda entry: entry.path)),
                courts=("ca6", "scotus"),
            )
            lock_path.write_text(render_lockfile(replace(
                lockfile, sources={"example": updated},
            )))
        self.source = replace(
            self.source, carries_courts=True, first_courts=("ca6", "scotus"),
        )

    def _seed_complete_stage(self) -> Path:
        lock_path = self.checkout / "corpus/lockfiles" / f"{self.labels[0]}.yaml"
        loaded = load_lockfile(lock_path, known_sources={"example": True})
        assert loaded.lockfile is not None
        pin = loaded.lockfile.sources["example"]
        entries = {entry.path: entry for entry in pin.entries}
        args = {
            "label": self.labels[0], "snapshot": f"example-{pin.snapshot_date}",
            "courts": list(pin.courts or ()),
            "inputs": {
                table: {"path": data_file(table, pin.snapshot_date),
                        "sha256": entries[data_file(table, pin.snapshot_date)].sha256}
                for table in worker_identity.STAGE_TABLES
            },
        }
        self.host._write_stage(91, args)
        return staging.work_directory(self.labels[0], "example", work_root=self.work)

    def run_install(
        self, label: str | None = None, *, sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> tuple[int, str, str]:
        self.host.current_label = label or self.labels[0]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = install.run_corpus_install(
                argparse.Namespace(label=label or self.labels[0]),
                host=self.host, rendered_dir=RENDERED, checkout=self.checkout,
                snapshots_root=self.snapshots, work_root=self.work, clock=lambda: NOW,
                sleep=sleep or (lambda _seconds: None),
                monotonic=monotonic or (lambda: 0.0),
                sources=(self.source,),
            )
        return code, out.getvalue(), err.getvalue()

    def _assert_removal_fix(self, out: str) -> None:
        self.assertIn(f"remove {self.file} and {self.record_file}", out.lower())
        self.assertIn(report.command("corpus install"), out)

    def test_ok_path_keeps_existing_file(self) -> None:
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertIn("preconditions: ok", out)
        self.assertIn(f"{self.labels[0]}: example 2099-01-02; not yet recorded", out)
        size = len(b"fictitious first snapshot")
        self.assertIn(f"fetch: ok — example: 1 files, {size} bytes; no fetches deferred", out)
        self.assertIn("verify: ok — 1 files match fetch records and pinned sidecars", out)
        self.assertIn(f"record: ok — {self.labels[0]}: installing", out)
        self.assertIn(
            f"retain: ok — 1 kept ({self.labels[0]}), 0 removed, 1 unpinned", out,
        )
        self.assertEqual(self.host.record_rows[self.labels[0]]["state"], "installing")
        self.assertEqual(self.host.lock_releases, 1)

    def test_stage_rows_follow_record_and_summarize_the_deferred_job(self) -> None:
        self._enable_stage()
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertLess(out.index("record: ok"), out.index("stage: ok"))
        self.assertLess(out.index("stage: ok"), out.index("ingest: ok"))
        self.assertLess(out.index("ingest: ok"), out.index("retain: ok"))
        self.assertIn("ca6: dockets 1, opinion-clusters 1, citations 1, opinions 1", out)
        self.assertIn("scotus: dockets 2, opinion-clusters 2, citations 2, opinions 2", out)
        self.assertIn("example: 2 courts staged, 15 records read, 8 seconds", out)
        self.assertEqual(len(self.host.stage_deferred), 1)
        self.assertEqual(self.host.stage_deferred[0]["courts"], ["ca6", "scotus"])
        self.assertEqual(tuple(self.host.stage_deferred[0]["inputs"]), worker_identity.STAGE_TABLES)
        self.assertEqual([args["court"] for args in self.host.caselaw_deferred], ["ca6", "scotus"])
        self.assertEqual(self.host.caselaw_events[:2], [("defer", "ca6"), ("defer", "scotus")])
        self.assertTrue(all(set(args) == {"label", "snapshot", "court", "courts"}
                            for args in self.host.caselaw_deferred))
        loaded = courts.load_court_map(self.checkout / "courts.yaml")
        assert loaded.court_map is not None
        expected_geography = {
            identifier: {
                "level": item.level, "circuit": item.circuit, "state": item.state,
            }
            for identifier in self.host.stage_deferred[0]["courts"]
            if (item := loaded.court_map.court(identifier)) is not None
        }
        self.assertTrue(all(
            args["courts"] == expected_geography for args in self.host.caselaw_deferred
        ))

    def test_complete_matching_stage_prints_courts_and_defers_nothing(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertIn("ca6: dockets 1, opinion-clusters 1, citations 1, opinions 1", out)
        self.assertIn("scotus: dockets 2, opinion-clusters 2, citations 2, opinions 2", out)
        self.assertIn("example: complete; nothing deferred", out)
        self.assertEqual(self.host.stage_deferred, [])
        again, rerun, _ = self.run_install()
        self.assertEqual(again, 0, rerun)
        self.assertIn("example: complete; nothing deferred", rerun)
        self.assertEqual(self.host.stage_deferred, [])

    def test_ingest_waits_once_per_round_and_reports_a_minute_heartbeat(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        now = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += snapshots.HEARTBEAT_SECONDS

        code, out, err = self.run_install(sleep=sleep, monotonic=lambda: now[0])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sleeps, [1.0])
        self.assertIn(
            f"ingest: ok — caselaw: ingesting, 2 courts, {snapshots.HEARTBEAT_SECONDS} seconds",
            out,
        )
        self.assertIn(
            f"ingest: ok — caselaw: 2 courts, 3 opinions, 3 ready, 0 failed, "
            f"{snapshots.HEARTBEAT_SECONDS} seconds",
            out,
        )
        self.assertEqual(
            self.host.caselaw_events[:6],
            [("defer", "ca6"), ("defer", "scotus"),
             ("read", "ca6"), ("read", "scotus"),
             ("read", "ca6"), ("read", "scotus")],
        )

    def test_ingest_prints_exact_court_rows_and_summary(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        cite_types = {"case_cite": 3, "statute": 1, "court_rule": 1}
        cite_forms = {"full": 2, "short": 1}
        self.host.caselaw_counts = {
            "ca6": {
                "opinions": 1, "by_status": {"failed": 1},
                "by_text_source": {}, "by_precedential": {"unknown": 1},
                "by_failure_reason": {"no-text": 1},
            },
            "scotus": {
                "opinions": 2, "by_status": {"ready": 2},
                "by_text_source": {"xml_harvard": 1, "html_columbia": 1},
                "by_precedential": {"published": 1, "unpublished": 1},
                "by_failure_reason": {},
            },
        }
        self.host.caselaw_section_counts = {
            "ca6": {
                "ready": 0, "sectioned": 0,
                "sections_by_type": {}, "chars_by_type": {},
            },
            "scotus": {
                "ready": 2, "sectioned": 2,
                "sections_by_type": {"majority": 3, "footnote": 1},
                "chars_by_type": {"majority": 12, "footnote": 8},
            },
        }
        self.host.caselaw_anchor_counts = {
            "ca6": {"ready": 0, "anchored": 0, "by_text_source": {}},
            "scotus": {
                "ready": 2, "anchored": 2,
                "by_text_source": {
                    "html_columbia": {
                        "documents": 1, "with_anchors": 0,
                        "chars": 0, "anchored_chars": 0,
                    },
                    "xml_harvard": {
                        "documents": 1, "with_anchors": 1,
                        "chars": 20, "anchored_chars": 13,
                    },
                },
            },
        }
        self.host.caselaw_citation_counts = {
            "ca6": {
                "ready": 0, "cited": 0, "edges": 0,
                "by_type": {}, "by_form": {}, "case_rows": 0, "case_resolved": 0,
            },
            "scotus": {
                "ready": 2, "cited": 2, "edges": 5,
                "by_type": cite_types, "by_form": cite_forms,
                "case_rows": 3, "case_resolved": 2,
            },
        }
        treatment_signals = {"overruled": 2, "vacated": 1}
        treatment_outcomes = {"negative": 1, "caution": 1, "lineage_unverified": 1}
        treatment_qualifiers = {"none": 2, "in_part": 1}
        self.host.caselaw_treatment_counts = {
            "ca6": {
                "ready": 0, "treated": 0, "signalled": 0,
                "by_signal": {}, "by_outcome": {}, "by_qualifier": {},
                "by_effective_section": {},
            },
            "scotus": {
                "ready": 2, "treated": 2, "signalled": 3,
                "by_signal": treatment_signals,
                "by_outcome": treatment_outcomes,
                "by_qualifier": treatment_qualifiers,
                "by_effective_section": {"majority": 3},
            },
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        section_shares = {"majority": "60 % (3)", "footnote": "40 % (1)"}
        rows = [line for line in out.splitlines() if line.startswith("ingest: ok — ")]
        self.assertEqual(rows, [
            "ingest: ok — ca6: 1 opinions; ready 0, failed 1 "
            "(no-text 1, unparseable 0, empty 0, interrupted 0); "
            "xml_harvard 0, html_columbia 0, html_lawbox 0, html_anon_2020 0, "
            "html 0, plain_text 0; published 0, unpublished 0, unknown 1",
            "ingest: ok — ca6 sections: 0 of 0 ready documents; "
            + ", ".join(f"{kind} 0 % (0)" for kind in worker_identity.SECTION_TYPES),
            "ingest: ok — ca6 anchors: 0 of 0 ready documents anchored",
            "ingest: ok — ca6 citations: 0 of 0 ready documents; "
            "0 edges, 0.0 per document; case_cite 0 (0 % resolved; "
            + ", ".join(f"{form} 0" for form in worker_identity.CITE_FORMS)
            + "); "
            + ", ".join(f"{kind} 0" for kind in worker_identity.CITE_TYPES if kind != "case_cite"),
            "ingest: ok — ca6 treatment: 0 of 0 ready documents under "
            + worker_identity.PATTERN_SET_ID + "; 0 signalled edges; "
            + ", ".join(f"{kind} 0" for kind in worker_identity.TREATMENT_SIGNALS) + "; "
            + ", ".join(f"{kind} 0" for kind in (
                *worker_identity.TREATMENT_STATES, *worker_identity.NO_STATE_REASONS,
            )) + "; "
            + ", ".join(f"{kind} 0" for kind in worker_identity.QUALIFIERS),
            "ingest: ok — scotus: 2 opinions; ready 2, failed 0 "
            "(no-text 0, unparseable 0, empty 0, interrupted 0); "
            "xml_harvard 1, html_columbia 1, html_lawbox 0, html_anon_2020 0, "
            "html 0, plain_text 0; published 1, unpublished 1, unknown 0",
            "ingest: ok — scotus sections: 2 of 2 ready documents; "
            + ", ".join(
                f"{kind} {section_shares.get(kind, '0 % (0)')}"
                for kind in worker_identity.SECTION_TYPES
            ),
            "ingest: ok — scotus anchors: 2 of 2 ready documents anchored; "
            "xml_harvard 1 documents, 100 % with a page, 65 % of characters; "
            "html_columbia 1 documents, 0 % with a page, 0 % of characters",
            "ingest: ok — scotus citations: 2 of 2 ready documents; "
            "5 edges, 2.5 per document; case_cite 3 (67 % resolved; "
            + ", ".join(
                f"{form} {cite_forms.get(form, 0)}"
                for form in worker_identity.CITE_FORMS
            ) + "); "
            + ", ".join(
                f"{kind} {cite_types.get(kind, 0)}"
                for kind in worker_identity.CITE_TYPES if kind != "case_cite"
            ),
            "ingest: ok — scotus treatment: 2 of 2 ready documents under "
            + worker_identity.PATTERN_SET_ID + "; 3 signalled edges; "
            + ", ".join(
                f"{kind} {treatment_signals.get(kind, 0)}"
                for kind in worker_identity.TREATMENT_SIGNALS
            ) + "; "
            + ", ".join(
                f"{kind} {treatment_outcomes.get(kind, 0)}"
                for kind in (*worker_identity.TREATMENT_STATES, *worker_identity.NO_STATE_REASONS)
            ) + "; "
            + ", ".join(
                f"{kind} {treatment_qualifiers.get(kind, 0)}"
                for kind in worker_identity.QUALIFIERS
            ),
            "ingest: ok — caselaw: 2 courts, 3 opinions, 2 ready, 1 failed, 0 seconds",
        ])
        self.assertLess(out.index(rows[-1]), out.index("retain: ok"))

    def test_ingest_shortfall_refuses_with_both_counts_and_worker_logs(self) -> None:
        self._enable_stage()
        directory = self._seed_complete_stage()
        self.host.caselaw_counts["ca6"] = {
            "opinions": 0, "by_status": {}, "by_text_source": {},
            "by_precedential": {}, "by_failure_reason": {},
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: 0 opinions recorded; 1 staged", out)
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertNotIn("retain: ok", out)
        self.assertTrue(directory.exists())

    def test_ingest_refuses_ready_documents_without_sections(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.caselaw_section_counts["ca6"] = {
            "ready": 1, "sectioned": 0,
            "sections_by_type": {}, "chars_by_type": {},
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: 0 of 1 ready documents have sections", out)
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertNotIn("ca6 sections:", out)
        self.assertNotIn("caselaw: 2 courts,", out)
        self.assertNotIn("retain: ok", out)

    def test_ingest_section_count_problem_refuses_with_court_and_fix(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        issue = report.Problem("section read failed", "Run the fictitious fix.")
        with patch.object(install.caselaw, "read_section_counts", return_value=issue) as read:
            code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: section read failed", out)
        self.assertIn(issue.fix, out)
        self.assertNotIn("retain: ok", out)
        read.assert_called_once_with(
            self.host, RENDERED, source="example", snapshot_date="2099-01-02",
            court="ca6", command_path=install.COMMAND_PATH,
        )

    def test_ingest_refuses_ready_documents_without_anchoring(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.caselaw_anchor_counts["ca6"] = {
            "ready": 1, "anchored": 0, "by_text_source": {},
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: 0 of 1 ready documents anchored", out)
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertNotIn("ca6 anchors:", out)
        self.assertNotIn("caselaw: 2 courts,", out)
        self.assertNotIn("retain: ok", out)

    def test_ingest_anchor_count_problem_refuses_with_court_and_fix(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        issue = report.Problem("anchor read failed", "Run the fictitious fix.")
        with patch.object(install.caselaw, "read_anchor_counts", return_value=issue) as read:
            code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: anchor read failed", out)
        self.assertIn(issue.fix, out)
        self.assertNotIn("retain: ok", out)
        read.assert_called_once_with(
            self.host, RENDERED, source="example", snapshot_date="2099-01-02",
            court="ca6", command_path=install.COMMAND_PATH,
        )

    def test_ingest_refuses_ready_documents_without_treatment_pass(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.caselaw_treatment_counts["ca6"] = {
            "ready": 1, "treated": 0, "signalled": 0,
            "by_signal": {}, "by_outcome": {}, "by_qualifier": {},
            "by_effective_section": {},
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: 0 of 1 ready documents treated under", out)
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertNotIn("ca6 treatment:", out)
        self.assertNotIn("retain: ok", out)

    def test_ingest_refuses_ready_documents_without_citation_pass(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.caselaw_citation_counts["ca6"] = {
            "ready": 1, "cited": 0, "edges": 0,
            "by_type": {}, "by_form": {}, "case_rows": 0, "case_resolved": 0,
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: 0 of 1 ready documents have citations parsed", out)
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertIn("ca6 sections:", out)
        self.assertIn("ca6 anchors:", out)
        self.assertNotIn("ca6 citations:", out)
        self.assertNotIn("caselaw: 2 courts,", out)
        self.assertNotIn("retain: ok", out)

    def test_ingest_citation_count_problem_refuses_with_court_and_fix(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        issue = report.Problem("citation read failed", "Run the fictitious fix.")
        with patch.object(install.caselaw, "read_citation_counts", return_value=issue) as read:
            code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: citation read failed", out)
        self.assertIn(issue.fix, out)
        self.assertNotIn("retain: ok", out)
        read.assert_called_once_with(
            self.host, RENDERED, source="example", snapshot_date="2099-01-02",
            court="ca6", command_path=install.COMMAND_PATH,
        )

    def test_failed_ingest_job_refuses_with_its_court_and_fix(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.caselaw_failure = "store"
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: caselaw store failed", out)
        self.assertIn(report.command("host provision --only disk-layout"), out)
        self.assertIn(report.command("apply"), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertNotIn("retain: ok", out)

    def test_ingest_count_problem_refuses_with_court_and_fix(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        issue = report.Problem("count read failed", "Run the fictitious fix.")
        with patch.object(install.caselaw, "read_counts", return_value=issue) as read_counts:
            code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse — ca6: count read failed", out)
        self.assertIn(issue.fix, out)
        self.assertNotIn("retain: ok", out)
        read_counts.assert_called_once_with(
            self.host, RENDERED, source="example", snapshot_date="2099-01-02",
            court="ca6", command_path=install.COMMAND_PATH,
        )

    def test_ingest_missing_or_unreadable_stage_record_refuses_with_its_fix(self) -> None:
        self._enable_stage()
        directory = self._seed_complete_stage()
        complete = staging.read_record(
            self.host, self.labels[0], "example", work_root=self.work,
            command_path=install.COMMAND_PATH,
        )
        self.assertIsInstance(complete, staging.StageRecord)
        for issue in (None, report.Problem("record damaged", "Repair the fictitious record.")):
            with self.subTest(issue=issue):
                with patch.object(install.staging, "read_record", side_effect=[complete, issue]) as read_record:
                    code, out, err = self.run_install()
                self.assertEqual(code, 1, out + err)
                self.assertIn("ingest: refuse", out)
                self.assertEqual(read_record.call_count, 2)
                self.assertEqual(self.host.caselaw_deferred, [])
                if issue is None:
                    self.assertIn(f"Remove {directory}", out)
                else:
                    self.assertIn(issue.fix, out)

    def test_interrupt_during_ingest_reports_the_worker_continues(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.interrupt_caselaw_job = True
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("ingest: refuse", out)
        self.assertIn("interrupted; the ingest continues in the worker", out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.lock_releases, 1)

    def test_disagreeing_stage_record_refuses_with_work_removal_fix(self) -> None:
        self._enable_stage()
        directory = self._seed_complete_stage()
        path = directory / worker_identity.STAGE_RECORD_NAME
        record = json.loads(path.read_text())
        record["inputs"]["opinions"]["sha256"] = "a" * 64
        path.write_text(json.dumps(record))
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("stage: refuse", out)
        self.assertIn("stage record disagrees with the lockfile", out)
        self.assertIn(f"Remove {directory}", out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.stage_deferred, [])

    def test_failed_stage_job_refuses_with_the_worker_logs_fix(self) -> None:
        self._enable_stage()
        self.host.stage_failure = "malformed"
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("stage: refuse", out)
        self.assertIn("opinions input is malformed", out)
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertEqual(len(self.host.stage_deferred), 1)

    def test_interrupt_during_stage_reports_the_worker_continues(self) -> None:
        self._enable_stage()
        self.host.interrupt_stage_job = True
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("stage: refuse", out)
        self.assertIn("interrupted; the stage continues in the worker", out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.lock_releases, 1)

    def test_missing_pinned_stage_input_refuses_before_defer(self) -> None:
        self._enable_stage(missing="opinions")
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("stage: refuse", out)
        self.assertIn(data_file("opinions", "2099-01-02"), out)
        self.assertIn(report.command("corpus cut"), out)
        self.assertEqual(self.host.stage_deferred, [])

    def test_empty_record_inserts_installing_with_fetch_and_verify_times(self) -> None:
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"record: ok — {self.labels[0]}: installing", out)
        self.assertEqual(self.host.record_writes, 1)
        assert self.host.last_payload is not None
        self.assertIs(self.host.last_payload["install"], True)
        self.assertEqual(self.host.last_payload["verified_at"], NOW.isoformat())
        self.assertEqual(self.host.last_payload["sources"][0]["fetched_at"], NOW.isoformat())
        self.assertEqual(self.host.snapshot_rows[("example", "2099-01-02")]["fetched_at"], NOW.isoformat())
        self.assertEqual(self.host.snapshot_rows[("example", "2099-01-02")]["verified_at"], NOW.isoformat())

    def test_held_cut_moves_to_installing(self) -> None:
        self._hold_record("cut")
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"record: ok — {self.labels[0]}: installing", out)
        self.assertEqual(self.host.record_rows[self.labels[0]]["state"], "installing")
        self.assertIsNone(self.host.record_rows[self.labels[0]]["installed_at"])

    def test_held_installing_and_installed_states_stay(self) -> None:
        for state in ("installing", "installed"):
            with self.subTest(state=state):
                self._hold_record(state)
                code, out, err = self.run_install()
                self.assertEqual(code, 0, out + err)
                self.assertIn(f"record: ok — {self.labels[0]}: {state}", out)
                self.assertEqual(self.host.record_rows[self.labels[0]]["state"], state)
                self.assertIsNone(self.host.record_rows[self.labels[0]]["installed_at"])

    def test_disagreeing_record_refuses_at_record_without_state_move(self) -> None:
        for conflict in ("label", "snapshot"):
            with self.subTest(conflict=conflict):
                self._hold_record("cut")
                if conflict == "label":
                    self.host.record_rows[self.labels[0]]["reason"] = "quarterly"
                else:
                    digest = self.host.snapshot_rows[("example", "2099-01-02")]["sidecar_sha256"]
                    self.host.snapshot_rows[("example", "2099-01-02")]["sidecar_sha256"] = (
                        ("0" if digest[0] != "0" else "1") + digest[1:]
                    )
                code, out, err = self.run_install()
                self.assertEqual(code, 1, out + err)
                self.assertIn("record: refuse", out)
                self.assertIn(self.labels[0], out)
                self.assertIn("Restore the matching lockfile", out)
                self.assertIn(report.command("corpus install"), out)
                self.assertEqual(self.host.record_rows[self.labels[0]]["state"], "cut")
                self.assertIsNone(self.host.last_payload)

    def test_superseded_race_refuses_inside_transaction(self) -> None:
        self._hold_record("cut")
        self.host.supersede_on_write = True
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("preconditions: ok", out)
        self.assertIn("record: refuse", out)
        self.assertIn(self.labels[0], out)
        self.assertIn("superseded", out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.record_writes, 1)
        self.assertIsNone(self.host.last_payload)

    def test_label_grammar_refuses_before_root(self) -> None:
        self.host.euid = 1000
        code, out, err = self.run_install("wrong")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("corpus-YYYY-MM-DD", err)
        self.assertEqual(self.host.calls, [])

    def test_absent_label_names_committed_labels(self) -> None:
        code, out, err = self.run_install("corpus-2099-01-09")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        for label in self.labels:
            self.assertIn(label, err)
        self.assertEqual(self.host.lock_releases, 1)

    def test_superseded_row_refuses_with_newest_label(self) -> None:
        self._hold_record("superseded")
        code, out, err = self.run_install()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("superseded", err)
        self.assertIn(self.labels[1], err)

    def _derive_second_label(self) -> None:
        directory = self.checkout / "corpus/lockfiles"
        loaded = load_lockfile(directory / f"{self.labels[0]}.yaml",
                               known_sources={"example": self.source.carries_courts})
        assert loaded.lockfile is not None
        shutil.rmtree(directory / self.labels[1])
        shutil.copytree(directory / self.labels[0], directory / self.labels[1])
        (directory / f"{self.labels[1]}.yaml").write_text(render_lockfile(replace(
            loaded.lockfile, label=self.labels[1], base=self.labels[0],
        )))

    def test_derived_label_follows_its_base_onto_the_box(self) -> None:
        self._derive_second_label()
        code, out, err = self.run_install(self.labels[1])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(f"base {self.labels[0]} of lockfile {self.labels[1]} is not recorded", err)
        self.assertIn(f"corpus install {self.labels[0]}", err)
        self.assertIn(f"corpus install {self.labels[1]}", err)
        self.assertEqual(self.host.record_writes, 0)
        self.assertEqual(self.host.deferred_urls, [])
        self.assertFalse(any(command[0] == "sha256sum" for command in self.host.calls))
        self.assertIsNone(self.host.lock_holder)

        self._hold_record("installing")
        code, out, err = self.run_install(self.labels[1])
        self.assertEqual(code, 0, out + err)
        self.assertIn(
            f"{self.labels[1]}: example 2099-01-02; not yet recorded; "
            f"base {self.labels[0]} installing",
            out,
        )
        self.assertEqual(self.host.record_rows[self.labels[1]]["base"], self.labels[0])
        self.assertEqual(self.host.record_rows[self.labels[1]]["state"], "installing")

    def test_root_refuses(self) -> None:
        self.host.euid = 1000
        code, out, err = self.run_install()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("root privileges", err)
        self.assertEqual(self.host.calls, [])

    def test_held_lock_refuses_and_success_releases(self) -> None:
        holder = backuplock.Record("gideon corpus cut", os.getpid() + 1, NOW)
        self.host.lock_holder = holder.to_json()
        code, out, err = self.run_install()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("gideon corpus cut", err)
        self.assertIn("corpus lock", err)
        self.assertEqual(self.host.lock_releases, 0)
        self.host.lock_holder = None
        self.assertEqual(self.run_install()[0], 0)
        self.assertEqual(self.host.lock_releases, 1)

    def test_short_file_with_whole_record_refuses_before_hash_or_defer(self) -> None:
        self.file.write_bytes(self.pinned_bytes[:-1])
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("fetch: refuse", out)
        self.assertIn(str(self.file), out)
        self._assert_removal_fix(out)
        self.assertEqual(self.host.deferred_urls, [])
        self.assertFalse(any(call[0] == "sha256sum" for call in self.host.calls))

    def test_existing_other_bytes_refuses_with_removal_only(self) -> None:
        other = bytes([self.pinned_bytes[0] ^ 1]) + self.pinned_bytes[1:]
        self.file.write_bytes(other)
        self.record_file.write_text(json.dumps(_whole_record(
            other, f"{self.source.base_url}{FILE_PATH}", 2,
        )))
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("verify: refuse", out)
        self.assertIn(str(self.file), out)
        self.assertIn("pinned sidecar", out)
        self._assert_removal_fix(out)
        self.assertNotIn("mirror_url", out)
        self.assertEqual(self.host.deferred_urls, [])

    def test_absent_file_fetches_from_base_and_verifies(self) -> None:
        self._remove_snapshot()
        base_file_url = f"{self.source.base_url}{FILE_PATH}"
        self.host.served_bytes[base_file_url] = self.pinned_bytes
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 fetch deferred", out)
        self.assertIn("verify: ok — 1 files match fetch records and pinned sidecars", out)
        self.assertEqual(self.host.deferred_urls, [base_file_url])
        self.assertEqual(tuple(self.host.job_polls.values()), (2,))
        self.assertEqual(self.file.read_bytes(), self.pinned_bytes)

    def test_absent_file_fetches_from_mirror_and_verifies(self) -> None:
        self._set_mirror()
        self._remove_snapshot()
        mirror_file_url = f"{self.mirror_url}{FILE_PATH}"
        self.host.served_bytes[mirror_file_url] = self.pinned_bytes
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 fetch deferred", out)
        self.assertIn("verify: ok — 1 files match fetch records and pinned sidecars", out)
        self.assertEqual(self.host.deferred_urls, [mirror_file_url])
        self.assertEqual(tuple(self.host.job_polls.values()), (2,))
        self.assertEqual(self.file.read_bytes(), self.pinned_bytes)

    def test_repin_then_remove_and_fetch_from_mirror(self) -> None:
        self._remove_snapshot()
        base_file_url = f"{self.source.base_url}{FILE_PATH}"
        mirror_file_url = f"{self.mirror_url}{FILE_PATH}"
        other = bytes([self.pinned_bytes[0] ^ 1]) + self.pinned_bytes[1:]
        self.host.served_bytes[base_file_url] = other
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("verify: refuse", out)
        self.assertIn("pinned sidecar", out)
        self.assertIn(f"corpus/lockfiles/{self.labels[0]}.yaml", out)
        self.assertIn("mirror_url", out)
        self.assertIn("new cut", out)
        self._assert_removal_fix(out)
        self.assertLess(out.index("mirror_url"), out.index(f"remove {self.file}"))
        self.assertEqual(self.host.deferred_urls, [base_file_url])

        self._set_mirror()
        self._remove_snapshot()
        self.host.served_bytes[mirror_file_url] = self.pinned_bytes
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 fetch deferred", out)
        self.assertIn("verify: ok — 1 files match fetch records and pinned sidecars", out)
        self.assertEqual(self.host.deferred_urls, [base_file_url, mirror_file_url])
        self.assertEqual(self.file.read_bytes(), self.pinned_bytes)

    def test_present_file_without_record_refuses_before_defer(self) -> None:
        self.record_file.unlink()
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("fetch: refuse", out)
        self.assertIn(str(self.file), out)
        self._assert_removal_fix(out)
        self.assertEqual(self.host.deferred_urls, [])

    def test_three_successive_installs_keep_previous_and_remove_older(self) -> None:
        third = self._add_third_lockfile()
        first, second = self.labels[:2]
        first_dir = self.snapshots / "example-2099-01-02"
        self.assertEqual(self.run_install()[0], 0)
        self.host.record_rows[first].update(state="installed", installed_at="2099-01-06T00:00:00+00:00")
        self._hold_record("cut", second)
        code, out, err = self.run_install(second)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"2 kept ({first}, {second}), 0 removed, 1 unpinned", out)

        self.host.record_rows[first]["state"] = "superseded"
        self.host.record_rows[second].update(
            state="installed", installed_at="2099-01-07T00:00:00+00:00",
        )
        self._hold_record("cut", third)
        code, out, err = self.run_install(third)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"3 kept ({first}, {second}, {third}), 0 removed, 0 unpinned", out)

        self.host.record_rows[second]["state"] = "superseded"
        self.host.record_rows[third].update(
            state="installed", installed_at="2099-01-08T00:00:00+00:00",
        )
        code, out, err = self.run_install(third)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"retain: ok — removed {first_dir}", out)
        self.assertIn(f"2 kept ({second}, {third}), 1 removed, 0 unpinned", out)
        self.assertFalse(first_dir.exists())
        code, out, err = self.run_install(third)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"2 kept ({second}, {third}), 0 removed, 0 unpinned", out)
        code, out, err = self.run_install(second)
        self.assertEqual(code, 1, out + err)
        self.assertIn("superseded", err)
        self.assertIn(report.command(f"corpus install {third}"), err)

    def test_unpinned_and_other_names_left_and_resolve_ignored(self) -> None:
        for name in ("orphan-2099-01-08", "stray-name", worker_identity.RESOLVE_DIR):
            (self.snapshots / name).mkdir()
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 kept", out)
        self.assertIn("0 removed, 3 unpinned", out)
        for name in ("orphan-2099-01-08", "stray-name", worker_identity.RESOLVE_DIR):
            self.assertTrue((self.snapshots / name).exists())

    def test_shared_snapshot_waits_for_every_binding_to_be_superseded(self) -> None:
        third = self._add_third_lockfile()
        first, second = self.labels[:2]
        first_lock = load_lockfile(
            self.checkout / "corpus/lockfiles" / f"{first}.yaml",
            known_sources={"example": self.source.carries_courts},
        ).lockfile
        assert first_lock is not None
        second_lock_path = self.checkout / "corpus/lockfiles" / f"{second}.yaml"
        second_lock_path.write_text(render_lockfile(replace(first_lock, label=second)))
        first_companion = self.checkout / "corpus/lockfiles" / first
        second_companion = self.checkout / "corpus/lockfiles" / second
        shutil.rmtree(second_companion)
        shutil.copytree(first_companion, second_companion)
        shutil.rmtree(self.snapshots / "example-2099-01-03")
        self._hold_record("superseded", first)
        self._hold_record("cut", second)
        self._hold_record("installed", third)
        shared_dir = self.snapshots / "example-2099-01-02"
        code, out, err = self.run_install(third)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(shared_dir.exists())
        self.assertIn("2 kept", out)
        self.host.record_rows[first]["installed_at"] = "2099-01-06T00:00:00+00:00"
        self.host.record_rows[second]["state"] = "superseded"
        code, out, err = self.run_install(third)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(shared_dir.exists())
        self.assertIn("2 kept", out)
        self.host.record_rows[first]["installed_at"] = None
        code, out, err = self.run_install(third)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"removed {shared_dir}", out)
        self.assertFalse(shared_dir.exists())

    def test_superseded_without_installed_at_is_not_previous(self) -> None:
        self._hold_record("superseded")
        first_dir = self.snapshots / "example-2099-01-02"
        code, out, err = self.run_install(self.labels[1])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"removed {first_dir}", out)
        self.assertIn(f"1 kept ({self.labels[1]}), 1 removed, 0 unpinned", out)

    def test_unreadable_root_refuses_with_provision_fix(self) -> None:
        self.host.unreadable_root = True
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("retain: refuse", out)
        self.assertIn(report.command("host provision"), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.lock_releases, 1)

    def test_interrupt_during_retain_reports_partial_removal(self) -> None:
        self._hold_record("superseded")
        self.host.interrupt_rmtree = True
        code, out, err = self.run_install(self.labels[1])
        self.assertEqual(code, 1, out + err)
        self.assertIn("retain: refuse", out)
        self.assertIn("directory removed in part is removed whole by the next run", out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.lock_releases, 1)

    def test_interrupt_during_fetch_reports_downloads_continue(self) -> None:
        self._remove_snapshot()
        self.host.served_bytes[f"{self.source.base_url}{FILE_PATH}"] = self.pinned_bytes
        self.host.interrupt_job = True
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("fetch: refuse", out)
        self.assertIn("downloads continue in the worker", out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.lock_releases, 1)
