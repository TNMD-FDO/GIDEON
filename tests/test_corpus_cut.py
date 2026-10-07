"""Corpus cut stages over a fictitious source and recorded host operations."""

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
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from gideon.cli import main
from gideon.host import backuplock, fetch, report, stack, worker
from gideon.host.corpus import cut, resolve
from gideon.host.corpus.lockfile import load_lockfile, render_errors
from gideon.host.corpus.sources import (
    IndexRequest,
    SourceDefinition,
    SourceEntry,
    SourceResolution,
)
from gideon.host.egress import load_egress_allowlist
from gideon.host.render import worker as worker_identity
from gideon.host.sysio import Command, PathLike, RealHost

ROOT = Path(__file__).resolve().parent.parent
RENDERED = "/rendered"
SNAPSHOT_DATE = "2099-01-02"
CUT_AT = datetime(2099, 1, 3, 4, 5, 6, tzinfo=UTC)
FILE_PATH = "objects/example.txt"
FILE_BYTES = b"example"
INDEX_BYTES = b"<listing>fictitious example</listing>\n"


class FakeHost(RealHost):
    """A temporary filesystem with recorded Compose, worker, and hash commands."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []
        self.writes: list[str] = []
        self.hash_code = 0
        self.euid = 0
        self.health = "healthy"
        self.snapshots = Path("/unused")
        self.index_bytes = INDEX_BYTES
        self.fetch_bytes = FILE_BYTES
        self.job_destinations: dict[int, str] = {}
        self.job_polls: dict[int, int] = {}
        self.next_job = 100
        self.kept_wait_polls = 0
        self.resolve_wait_polls = 0
        self.failed_destination: str | None = None
        self.record_row: dict[str, object] | None = None
        self.record_writes = 0
        self.record_code = 0
        self.interrupt_at: str | None = None
        self.lock_holder: str | None = None
        self.lock_releases = 0
        self.checkout = Path("/unused")
        self.owner_override: tuple[int, int] | None = None
        self.ownership: dict[str, tuple[int, int]] = {}
        self.chown_calls: list[tuple[str, int, int]] = []

    def mkdir(
        self, path: PathLike, *, mode: int = 0o755,
        parents: bool = False, exist_ok: bool = False,
    ) -> None:
        if str(path) != backuplock.LOCK_DIR:
            super().mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    def take_lock(self, path: PathLike, record: str) -> str | None:
        if str(path) != backuplock.CORPUS_LOCK.path:
            raise AssertionError(f"unexpected lock: {path}")
        if self.lock_holder is not None:
            return self.lock_holder
        self.lock_holder = record
        return None

    def release_lock(self, path: PathLike) -> None:
        if str(path) != backuplock.CORPUS_LOCK.path:
            raise AssertionError(f"unexpected lock: {path}")
        self.lock_holder = None
        self.lock_releases += 1

    def stat(self, path: PathLike) -> os.stat_result:
        info = list(super().stat(path))
        owner = self.ownership.get(str(path))
        if owner is None and str(path) == str(self.checkout):
            owner = self.owner_override
        if owner is not None:
            info[4], info[5] = owner
        return os.stat_result(info)

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.ownership[str(path)] = (uid, gid)
        self.chown_calls.append((str(path), uid, gid))

    def _bound(self, sql: str, name: str) -> str:
        match = re.search(rf"\\set {name} '([^']*)'", sql)
        assert match is not None, sql
        return match.group(1)

    def _write_fetch(self, job: int, destination: str, url: str, form: str) -> None:
        target = self.snapshots / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        if destination == self.failed_destination:
            failure = {
                "schema": 1, "job": job, "reason": "refused-host",
                "host": "blocked.example.test", "status": 403,
                "error": None, "at": CUT_AT.isoformat(),
            }
            (self.snapshots / f"{destination}{worker_identity.FAILURE_SUFFIX}").write_text(
                json.dumps(failure)
            )
            return
        content = self.index_bytes if form == worker_identity.FRESH_FORM else self.fetch_bytes
        target.write_bytes(content)
        parsed = urlsplit(url)
        record = {
            "schema": 1, "state": "whole", "form": form,
            "host": parsed.hostname, "path": parsed.path,
            "etag": None, "last_modified": None,
            "total": len(content), "durable": len(content), "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "fetched_at": CUT_AT.isoformat(), "job": job,
            "seconds": 0.1, "resumes": 0,
        }
        (self.snapshots / f"{destination}{worker_identity.RECORD_SUFFIX}").write_text(
            json.dumps(record)
        )

    def geteuid(self) -> int:
        return self.euid

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        if str(path) == f"{RENDERED}/compose.yaml":
            return f"services:\n  {worker_identity.WORKER_SERVICE_NAME}: {{}}\n"
        return super().read_text(path, encoding=encoding)

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
        del check, cwd, env, timeout, passthrough
        command = list(argv)
        self.calls.append(command)
        if command == stack.compose_argv(RENDERED, "ps", "--all", "--format", "json"):
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "Service": worker_identity.WORKER_SERVICE_NAME,
                "State": "running",
                "Health": self.health,
            }), "")
        if command == worker.psql_argv(RENDERED):
            assert input is not None
            if "procrastinate_defer_jobs_v1" in input:
                args = json.loads(self._bound(input, "v_args"))
                job = self.next_job
                self.next_job += 1
                destination = args["destination"]
                self.job_destinations[job] = destination
                self.job_polls[job] = 0
                self._write_fetch(job, destination, args["url"], args["form"])
                return subprocess.CompletedProcess(command, 0, f"{job}\n", "")
            if "FROM procrastinate_jobs" in input:
                job = int(self._bound(input, "v_job_id"))
                destination = self.job_destinations[job]
                if self.interrupt_at == "fetch" and not destination.startswith("resolve/"):
                    raise KeyboardInterrupt
                polls = self.job_polls[job]
                self.job_polls[job] += 1
                wait = (self.resolve_wait_polls if destination.startswith("resolve/")
                        else self.kept_wait_polls)
                status = "doing" if polls < wait else (
                    "failed" if destination == self.failed_destination else "succeeded"
                )
                return subprocess.CompletedProcess(command, 0, f"{job}|{status}|1\n", "")
            if "DO $cut$" in input:
                self.record_writes += 1
                if self.record_code:
                    return subprocess.CompletedProcess(
                        command, self.record_code, "",
                        f"ERROR: CORPUS_LOCKFILE_CONFLICT:corpus-{CUT_AT.date().isoformat()}\n",
                    )
                payload = json.loads(self._bound(input, "v_payload"))
                fixed = {
                    key: payload[key] for key in (
                        "label", "schema", "pipeline", "cut_at", "reason", "base",
                    )
                }
                fixed["sources"] = [
                    {"source": pin["source"], "snapshot_date": pin["snapshot_date"]}
                    for pin in payload["sources"]
                ]
                if self.record_row is not None and self.record_row["label"] == payload["label"] and any(
                    self.record_row[key] != value for key, value in fixed.items()
                ):
                    return subprocess.CompletedProcess(
                        command, 3, "",
                        f"ERROR: CORPUS_LOCKFILE_CONFLICT:{payload['label']}\n",
                    )
                self.record_row = {**fixed,
                    "installed_at": None, "state": "cut",
                }
                return subprocess.CompletedProcess(command, 0, "", "")
            if "jsonb_build_object" in input:
                requested = self._bound(input, "v_label")
                output = (
                    json.dumps(self.record_row) + "\n"
                    if self.record_row and self.record_row["label"] == requested else ""
                )
                return subprocess.CompletedProcess(command, 0, output, "")
            raise AssertionError(f"unexpected psql statement: {input}")
        if command[0] == "sha256sum":
            if self.interrupt_at == "verify":
                raise KeyboardInterrupt
            if self.hash_code:
                return subprocess.CompletedProcess(command, self.hash_code, "", "hash tool failed")
            path = Path(command[1])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return subprocess.CompletedProcess(command, 0, f"{digest}  {path}\n", "")
        raise AssertionError(f"unexpected command: {command}")

    def write_text(
        self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644
    ) -> None:
        self.writes.append(str(path))
        super().write_text(path, text, encoding=encoding, mode=mode)

    def write_bytes(self, path: PathLike, data: bytes, *, mode: int = 0o644) -> None:
        self.writes.append(str(path))
        super().write_bytes(path, data, mode=mode)


class Cut(unittest.TestCase):
    """A cut hashes local bytes independently of its fetch record and old pin."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.checkout = root / "checkout"
        self.snapshots = root / "snapshots"
        (self.checkout / "config").mkdir(parents=True)
        shutil.copy2(ROOT / "courts.yaml", self.checkout / "courts.yaml")
        shutil.copy2(ROOT / "config/egress.yaml", self.checkout / "config/egress.yaml")
        allowlist = load_egress_allowlist(self.checkout / "config/egress.yaml")
        assert allowlist.allowlist is not None
        group = allowlist.allowlist.group("corpus")
        assert group is not None
        host_name = group.hosts[0].host
        self.source = SourceDefinition(
            "example", f"https://{host_name}/archive/", True, ("ca6",),
            lambda: (IndexRequest("listing.xml", f"https://{host_name}/listing"),),
            lambda documents: SourceResolution(
                SNAPSHOT_DATE, (SourceEntry(FILE_PATH, f"https://{host_name}/archive/{FILE_PATH}"),)
            ),
        )
        self.host = FakeHost()
        self.host.snapshots = self.snapshots
        self.host.checkout = self.checkout
        self.destination = fetch.snapshot_destination("example", SNAPSHOT_DATE, FILE_PATH)
        self.file = self.snapshots / self.destination
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(FILE_BYTES)
        self.write_record(FILE_BYTES)

    def write_record(self, content: bytes) -> None:
        record = {
            "schema": 1, "state": "whole", "form": worker_identity.KEPT_FORM,
            "host": urlsplit(self.source.base_url).hostname,
            "path": "/archive/" + FILE_PATH,
            "etag": None, "last_modified": None,
            "total": len(content), "durable": len(content), "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "fetched_at": CUT_AT.isoformat(), "job": 1, "seconds": 0.1, "resumes": 0,
        }
        record_path = self.snapshots / f"{self.destination}{worker_identity.RECORD_SUFFIX}"
        record_path.write_text(json.dumps(record))

    def run_cut(self, *, base: str | None = None, add_courts: str | None = None,
                now: list[float] | None = None,
                cut_at: datetime = CUT_AT) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        elapsed = now if now is not None else [0.0]

        def advance(seconds: float) -> None:
            elapsed[0] += seconds

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cut.run_corpus_cut(
                argparse.Namespace(base=base, add_courts=add_courts),
                host=self.host, rendered_dir=RENDERED, checkout=self.checkout,
                snapshots_root=self.snapshots, clock=lambda: cut_at,
                sleep=advance, monotonic=lambda: elapsed[0],
                sources=(self.source,),
            )
        return code, out.getvalue(), err.getvalue()

    def test_writes_loadable_lockfile_and_reports_stages(self) -> None:
        code, out, err = self.run_cut()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertIn("preconditions: ok", out)
        self.assertIn("resolve: ok", out)
        self.assertIn("fetch: ok", out)
        self.assertIn("no fetches deferred", out)
        self.assertIn("verify: ok", out)
        self.assertIn("lockfile: ok", out)
        self.assertIn(f"record: ok — corpus-{CUT_AT.date().isoformat()}: cut", out)
        self.assertEqual(self.host.record_writes, 1)
        path = self.checkout / "corpus/lockfiles" / f"corpus-{CUT_AT.date().isoformat()}.yaml"
        loaded = load_lockfile(path, known_sources={self.source.name: True})
        self.assertTrue(loaded.ok, render_errors(loaded.errors))
        self.assertEqual((path.with_suffix("") / "example.listing.xml").read_bytes(), INDEX_BYTES)
        self.assertEqual(len(self.host.writes), 3)

    def test_second_run_writes_nothing(self) -> None:
        self.assertEqual(self.run_cut()[0], 0)
        first_writes = list(self.host.writes)
        self.host.record_row = None
        code, out, err = self.run_cut()
        self.assertEqual(code, 0, out + err)
        self.assertIn("unchanged", out)
        self.assertEqual(self.host.writes, first_writes)
        self.assertEqual(self.host.record_writes, 2)
        self.assertIsNotNone(self.host.record_row)

    def test_reason_priority_for_all_four_changes(self) -> None:
        self.assertEqual(self.run_cut()[0], 0)
        path = self.checkout / "corpus/lockfiles" / f"corpus-{CUT_AT.date().isoformat()}.yaml"
        loaded = load_lockfile(path, known_sources={"example": True})
        assert loaded.lockfile is not None
        old = loaded.lockfile
        pin = old.sources["example"]
        self.assertEqual(cut._cut_reason(old.sources, None), "tranche")
        self.assertEqual(cut._cut_reason({
            "example": replace(pin, courts=("ca6", "scotus")),
        }, old), "tranche")
        later = replace(pin, snapshot_date="2099-04-05")
        self.assertEqual(cut._cut_reason(
            {"caselaw": later}, replace(old, sources={"caselaw": pin})
        ), "quarterly")
        self.assertEqual(cut._cut_reason(old.sources,
                                         replace(old, pipeline="9.9.9")), "pipeline")
        self.assertEqual(cut._cut_reason({"example": later}, old), "instrument")

    def test_kept_snapshot_keeps_its_committed_index_bytes(self) -> None:
        self.assertEqual(self.run_cut()[0], 0)
        self.host.index_bytes = b"<listing>a moved listing</listing>\n"
        next_cut = CUT_AT + timedelta(days=1)
        with mock.patch.object(cut, "PIPELINE_VERSION", "0.0.1"):
            code, out, err = self.run_cut(cut_at=next_cut)
        self.assertEqual(code, 0, out + err)
        path = self.checkout / "corpus/lockfiles" / f"corpus-{next_cut.date().isoformat()}.yaml"
        loaded = load_lockfile(path, known_sources={"example": True})
        self.assertTrue(loaded.ok, render_errors(loaded.errors))
        assert loaded.lockfile is not None
        self.assertEqual(loaded.lockfile.reason, "pipeline")
        self.assertEqual((path.with_suffix("") / "example.listing.xml").read_bytes(), INDEX_BYTES)

    def test_refetch_fix_names_the_record_beside_the_file(self) -> None:
        self.file.write_bytes(b"EXAMPLE")
        code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn(f"Remove {self.file} and {self.file}{worker_identity.RECORD_SUFFIX}", out)

    def test_changed_snapshot_writes_new_label_with_instrument_reason(self) -> None:
        self.assertEqual(self.run_cut()[0], 0)
        next_snapshot = "2099-02-02"
        self.source = replace(self.source, read_index=lambda _documents: SourceResolution(
            next_snapshot, (SourceEntry(FILE_PATH, f"{self.source.base_url}{FILE_PATH}"),)
        ))
        self.destination = fetch.snapshot_destination("example", next_snapshot, FILE_PATH)
        self.file = self.snapshots / self.destination
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(FILE_BYTES)
        self.write_record(FILE_BYTES)
        next_cut = CUT_AT + timedelta(days=1)
        code, out, err = self.run_cut(cut_at=next_cut)
        self.assertEqual(code, 0, out + err)
        path = self.checkout / "corpus/lockfiles" / f"corpus-{next_cut.date().isoformat()}.yaml"
        loaded = load_lockfile(path, known_sources={"example": True})
        assert loaded.lockfile is not None
        self.assertEqual(loaded.lockfile.reason, "instrument")
        self.assertEqual(loaded.lockfile.sources["example"].snapshot_date, next_snapshot)

    def test_directory_label_is_used_once_before_any_write(self) -> None:
        self.assertEqual(self.run_cut()[0], 0)
        self.host.record_row = None
        before = list(self.host.writes)
        with mock.patch.object(cut, "PIPELINE_VERSION", "9.9.9"):
            code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn("label corpus-", out)
        self.assertIn("already has a lockfile", out)
        self.assertIn("later UTC date", out)
        self.assertNotIn("delete", out.lower())
        self.assertEqual(self.host.writes, before)

    def test_recorded_label_alone_is_used_once_before_any_write(self) -> None:
        self.assertEqual(self.run_cut()[0], 0)
        before = list(self.host.writes)
        shutil.rmtree(self.checkout / "corpus/lockfiles")
        code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn("label corpus-", out)
        self.assertIn("already recorded", out)
        self.assertIn("later UTC date", out)
        self.assertNotIn("delete", out.lower())
        self.assertEqual(self.host.writes, before)

    def test_recorded_label_with_different_rows_refuses(self) -> None:
        self.assertEqual(self.run_cut()[0], 0)
        assert self.host.record_row is not None
        self.host.record_row["reason"] = "quarterly"
        before = list(self.host.writes)
        code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn("record: refuse", out)
        self.assertIn("lockfile label corpus-", out)
        self.assertNotIn("delete", out.lower())
        self.assertEqual(self.host.writes, before)

    def test_unfinished_label_directory_is_replaced(self) -> None:
        companion = self.checkout / "corpus/lockfiles" / f"corpus-{CUT_AT.date().isoformat()}"
        companion.mkdir(parents=True)
        (companion / "stale.txt").write_text("incomplete")
        code, out, err = self.run_cut()
        self.assertEqual(code, 0, out + err)
        self.assertFalse((companion / "stale.txt").exists())
        self.assertEqual((companion / "example.listing.xml").read_bytes(), INDEX_BYTES)

    def test_lock_is_refused_when_held_and_released_after_a_run(self) -> None:
        holder = backuplock.Record(
            "gideon corpus cut", os.getpid() + 1, CUT_AT,
        )
        self.host.lock_holder = holder.to_json()
        code, out, err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("gideon corpus cut", err)
        self.assertIn("corpus cut lock", err)
        self.assertEqual(self.host.lock_releases, 0)
        self.host.lock_holder = None
        self.assertEqual(self.run_cut()[0], 0)
        self.assertIsNone(self.host.lock_holder)
        self.assertEqual(self.host.lock_releases, 1)

    def test_interrupts_name_the_stage_and_release_the_lock(self) -> None:
        cases = (
            ("fetch", "downloads continue in the worker", "rejoin"),
            ("verify", "nothing was recorded", "start again"),
        )
        for stage, detail, fix in cases:
            with self.subTest(stage=stage):
                self.host.interrupt_at = stage
                if stage == "fetch":
                    self.file.unlink()
                    (self.snapshots / f"{self.destination}{worker_identity.RECORD_SUFFIX}").unlink()
                code, out, _err = self.run_cut()
                self.assertEqual(code, 1)
                self.assertEqual(out.count(f"{stage}: refuse"), 1)
                self.assertIn(detail, out)
                self.assertIn(fix, out)
                self.assertIsNone(self.host.lock_holder)
                self.assertEqual(self.host.lock_releases, 1)
                self.host.interrupt_at = None
                if stage == "fetch":
                    self.file.write_bytes(FILE_BYTES)
                    self.write_record(FILE_BYTES)
                self.host.lock_releases = 0

    def test_written_files_take_lockfiles_directory_owner(self) -> None:
        self.host.owner_override = (12345, 23456)
        code, out, err = self.run_cut()
        self.assertEqual(code, 0, out + err)
        label = f"corpus-{CUT_AT.date().isoformat()}"
        base = self.checkout / "corpus/lockfiles"
        expected = (
            self.checkout / "corpus", base, base / label,
            base / label / "example.listing.xml",
            base / label / "example.sha256", base / f"{label}.yaml",
        )
        self.assertEqual(
            self.host.chown_calls,
            [(str(path), 12345, 23456) for path in expected],
        )

    def test_new_lockfiles_directory_takes_existing_corpus_owner(self) -> None:
        corpus = self.checkout / "corpus"
        corpus.mkdir()
        self.host.ownership[str(corpus)] = (34567, 45678)
        code, out, err = self.run_cut()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.host.chown_calls[0],
                         (str(corpus / "lockfiles"), 34567, 45678))
        self.assertTrue(all((uid, gid) == (34567, 45678)
                            for _path, uid, gid in self.host.chown_calls))

    def test_fetch_defers_only_missing_file_and_waits_for_it(self) -> None:
        missing_path = "objects/missing.txt"
        self.source = replace(self.source, read_index=lambda _documents: SourceResolution(
            SNAPSHOT_DATE, (
                SourceEntry(FILE_PATH, f"{self.source.base_url}{FILE_PATH}"),
                SourceEntry(missing_path, f"{self.source.base_url}{missing_path}"),
            ),
        ))
        self.host.kept_wait_polls = 2
        code, out, err = self.run_cut()
        self.assertEqual(code, 0, out + err)
        destinations = tuple(self.host.job_destinations.values())
        self.assertEqual(destinations, (
            fetch.resolve_destination("example/listing.xml"),
            fetch.snapshot_destination("example", SNAPSHOT_DATE, missing_path),
        ))
        self.assertIn(f"{missing_path}: {len(FILE_BYTES)} bytes", out)
        self.assertIn("1 fetch deferred", out)
        self.assertEqual((self.snapshots / destinations[1]).read_bytes(), FILE_BYTES)

    def run_cut_capturing_resolve(
        self, **kwargs: object
    ) -> tuple[int, str, list[resolve.ResolvedSource | resolve.ResolveFailure]]:
        results: list[resolve.ResolvedSource | resolve.ResolveFailure] = []
        original = resolve.resolve_source

        def capture(*args: object, **inner: object) -> resolve.ResolvedSource | resolve.ResolveFailure:
            result = original(*args, **inner)  # type: ignore[arg-type]
            results.append(result)
            return result

        with mock.patch.object(resolve, "resolve_source", side_effect=capture):
            code, out, _err = self.run_cut(**kwargs)  # type: ignore[arg-type]
        return code, out, results

    def test_failed_resolve_names_index_and_cleans_its_directory(self) -> None:
        self.host.failed_destination = fetch.resolve_destination("example/listing.xml")
        code, out, results = self.run_cut_capturing_resolve()
        self.assertEqual(code, 1)
        assert isinstance(results[0], resolve.ResolveFailure)
        self.assertEqual(results[0].reason, "refused-host")
        self.assertIn("resolve: refuse", out)
        self.assertIn("listing.xml", out)
        self.assertIn("blocked.example.test", out)
        self.assertIn("corpus group", out)
        self.assertFalse((self.snapshots / "resolve/example").exists())
        self.assertEqual(self.host.record_writes, 0)

    def test_resolve_wait_has_a_bound(self) -> None:
        self.host.resolve_wait_polls = resolve.RESOLVE_TIMEOUT_SECONDS + 2
        now = [0.0]
        code, out, results = self.run_cut_capturing_resolve(now=now)
        self.assertEqual(code, 1)
        assert isinstance(results[0], resolve.ResolveFailure)
        self.assertEqual(results[0].reason, "timeout")
        self.assertIn("did not finish before the bound", out)
        self.assertGreaterEqual(now[0], resolve.RESOLVE_TIMEOUT_SECONDS)
        self.assertFalse((self.snapshots / "resolve/example").exists())

    def test_reader_refusal_names_the_calling_command(self) -> None:
        source = replace(
            self.source,
            read_index=lambda _documents: report.Problem(
                "fictitious listing is unreadable",
                "Check the listing, then retry.",
            ),
        )
        result = resolve.ResolvedSource.from_index(
            source, {}, command_path="corpus watch"
        )
        assert isinstance(result, resolve.ResolveFailure)
        self.assertEqual(result.reason, "unreadable-index")
        self.assertEqual(
            result.problem.fix,
            f"Check the listing. Then run {report.command('corpus watch')} again.",
        )

    def test_refused_host_fetch_names_file_and_allowlist_group(self) -> None:
        self.file.unlink()
        (self.snapshots / f"{self.destination}{worker_identity.RECORD_SUFFIX}").unlink()
        self.host.failed_destination = self.destination
        code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn("fetch: refuse", out)
        self.assertIn(FILE_PATH, out)
        self.assertIn("blocked.example.test", out)
        self.assertIn("corpus group", out)
        self.assertIn("config/egress.yaml", out)
        self.assertEqual(self.host.record_writes, 0)

    def test_fetch_heartbeat_has_counts_and_bytes_alone(self) -> None:
        self.file.unlink()
        (self.snapshots / f"{self.destination}{worker_identity.RECORD_SUFFIX}").unlink()
        self.host.kept_wait_polls = cut.HEARTBEAT_SECONDS + 1
        code, out, err = self.run_cut()
        self.assertEqual(code, 0, out + err)
        self.assertIn("fetch: ok — 0/1 files, 0 bytes", out)
        self.assertIn(f"{FILE_PATH}: {len(FILE_BYTES)} bytes", out)

    def test_record_refusal_names_label_and_command(self) -> None:
        self.host.record_code = 3
        code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn("record: refuse", out)
        self.assertIn(f"corpus-{CUT_AT.date().isoformat()}", out)
        self.assertIn("corpus cut", out)
        self.assertEqual(self.host.record_writes, 1)

    def test_file_disagreeing_with_fetch_record_refuses(self) -> None:
        self.file.write_bytes(b"changed")
        code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn(str(self.file), out)
        self.assertIn("fetch record", out)
        self.assertIn("Remove", out)
        self.assertFalse((self.checkout / "corpus/lockfiles").exists())

    def test_short_file_disagreeing_with_fetch_record_names_removal(self) -> None:
        self.file.write_bytes(b"short")
        code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn(str(self.file), out)
        self.assertIn("fetch file does not match its record", out)
        self.assertIn(f"Remove {self.file}", out)

    def test_file_disagreeing_with_existing_sidecar_refuses(self) -> None:
        self.assertEqual(self.run_cut()[0], 0)
        changed = b"changed"
        self.file.write_bytes(changed)
        self.write_record(changed)
        code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn(str(self.file), out)
        self.assertIn("pinned sidecar", out)
        self.assertIn("Remove", out)

    def test_derived_flags_refuse_before_host_work(self) -> None:
        for values in ({"base": "corpus-2099-01-01"}, {"add_courts": "ca6"}):
            with self.subTest(values=values):
                code, out, err = self.run_cut(
                    base=values.get("base"), add_courts=values.get("add_courts"),
                )
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("derived corpus cut is not implemented yet", err)
                self.assertIn("corpus cut", err)
        self.assertEqual(self.host.calls, [])

    def test_hash_tool_failure_refuses_without_writing(self) -> None:
        self.host.hash_code = 127
        code, out, _err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertIn("sha256sum failed (exit 127)", out)
        self.assertEqual(self.host.writes, [])

    def test_preconditions_refuse_without_writing(self) -> None:
        self.host.euid = 1000
        code, out, err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("root privileges", err)
        self.assertIn("corpus cut", err)
        self.assertEqual(self.host.calls, [])

        self.host.euid = 0
        self.host.health = "unhealthy"
        code, out, err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("worker is not running and healthy", err)
        self.assertEqual(self.host.writes, [])
        self.assertIsNone(self.host.lock_holder)
        self.assertEqual(self.host.lock_releases, 1)

    def test_missing_artifact_and_unlisted_source_refuse(self) -> None:
        egress = self.checkout / "config/egress.yaml"
        egress.unlink()
        code, out, err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("egress allowlist is missing", err)
        shutil.copy2(ROOT / "config/egress.yaml", egress)

        self.source = replace(self.source, base_url="https://example.invalid/archive/")
        code, out, err = self.run_cut()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("outside the corpus allowlist", err)
        self.assertEqual(self.host.writes, [])


class Cli(unittest.TestCase):
    """The public parser dispatches both derived flags to the command refusal."""

    def test_derived_flags_reach_corpus_cut(self) -> None:
        for flag, value in (("--base", "corpus-2099-01-01"), ("--add-courts", "ca6")):
            with self.subTest(flag=flag):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main(["corpus", "cut", flag, value])
                self.assertEqual(code, 1)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("derived corpus cut is not implemented yet", err.getvalue())
