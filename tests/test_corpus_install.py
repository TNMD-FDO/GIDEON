"""Corpus install stages over fictitious committed lockfiles."""

import argparse
import contextlib
import copy
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
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID

import test_index_treatment
import yaml  # type: ignore[import-untyped]
from test_engine_verify import models_text

from gideon.host import (
    backuplock,
    courts,
    fetch,
    models,
    report,
    stack,
    staging,
    worker,
)
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
from gideon.host.index import (
    buildreport,
    generations,
    guards,
    reserve,
    smoke,
    treatment,
)
from gideon.host.index.record import GenerationRow, PromotedRow
from gideon.host.index.sample import sample_name
from gideon.host.render import worker as worker_identity
from gideon.host.render.ci import CI_ROOT
from gideon.host.render.engine import EMBED
from gideon.host.render.opensearch import OPENSEARCH_SERVICE_NAME
from gideon.host.render.qdrant import QDRANT_SERVICE_NAME
from gideon.host.sysio import Command, PathLike, RealHost

ROOT = Path(__file__).resolve().parent.parent
RENDERED = "/rendered"
NOW = datetime(2099, 1, 5, 4, 5, 6, tzinfo=UTC)
FILE_PATH = "objects/fictitious.txt"
SITE_PATH = "/etc/gideon/site.yaml"


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
        self.stage_counts_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
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
        self.caselaw_chunk_counts: dict[str, dict[str, object]] = {}
        self.interrupt_caselaw_job = False
        self.caselaw_events: list[tuple[str, str]] = []
        self.agreement_jobs: dict[int, dict[str, Any]] = {}
        self.agreement_deferred: list[dict[str, Any]] = []
        self.agreement_failure: str | None = None
        self.agreement_figures: dict[str, dict[str, int]] = {}
        self.agreement_events: list[tuple[str, str]] = []
        self.interrupt_agreement_job = False
        self.overruled_jobs: dict[int, dict[str, Any]] = {}
        self.overruled_deferred: list[dict[str, Any]] = []
        self.overruled_failure: str | None = None
        self.overruled_counts: dict[str, Any] = {
            "list_rows": 4, "rows": 6, "clusters": 3,
            "overruled": {"cite": 2, "name": 2, "ambiguous": 1, "absent": 1},
            "overruling": {"cite": 3, "docket": 1, "ambiguous": 1, "absent": 1},
        }
        self.overruled_events: list[str] = []
        self.overruled_count_reads: list[dict[str, str]] = []
        self.interrupt_overruled_job = False
        self.job_polls: dict[int, int] = {}
        self.next_job = 100
        self.unreadable_root = False
        self.interrupt_rmtree = False
        self.interrupt_job = False
        self.removed_dirs: list[Path] = []
        self.chown_calls: list[tuple[Path, int, int]] = []
        self.seed_writes: list[Path] = []
        self.site_text: str | None = (ROOT / "config/site.example.yaml").read_text() + "\nhardware_profile: 1x1v-1d\n"
        self.declared: tuple[str, ...] = (worker_identity.WORKER_SERVICE_NAME, OPENSEARCH_SERVICE_NAME, QDRANT_SERVICE_NAME, EMBED.service_name)
        self.health = dict.fromkeys(self.declared, "healthy")
        self.ps_reads = 0
        self.store_state_code = 0
        self.store_state_error: OSError | subprocess.SubprocessError | None = None
        self.store_state_text: str | None = None
        self.state_read = 2
        self.generations: dict[str, GenerationRow] = {}
        self.projection_counts = {"ready": 3, "chunked": 2, "chunks": 11}
        self.promoted: tuple[PromotedRow, ...] = ()
        self.free_bytes = 16_000_000
        self.df_text: str | None = None
        self.du_bytes = 2048
        self.du_text: str | None = None
        self.du_error: OSError | subprocess.SubprocessError | None = None
        self.footprints: list[dict[str, int]] = []
        self.jobs: dict[int, dict[str, Any]] = {}
        self.deferred: list[dict[str, Any]] = []
        self.live_id: int | None = None
        self.dense_live_id: int | None = None
        self.cluster_live_id: int | None = None
        self.task_by_job: dict[int, str] = {}
        self.active_task = worker_identity.LEXICAL_TASK
        self.pending_polls = 0
        self.dense_pending_polls = 0
        self.cluster_pending_polls = 0
        self.failure_reason: str | None = None
        self.dense_failure_reason: str | None = None
        self.cluster_failure_reason: str | None = None
        self.finish_stamp: str | None = NOW.isoformat()
        self.dense_finish_stamp: str | None = NOW.isoformat()
        self.cluster_finish_stamp: str | None = NOW.isoformat()
        self.cluster_counts = {"cluster_documents": 60, "cluster_chunks": 60, "cluster_texts": 35,
                               "cluster_clusters": 3, "cluster_members": 31, "cluster_grouped": 59,
                               "cluster_boilerplate": 2, "cluster_flagged": 56,
                               "cluster_seconds": 9, "cluster_reused": 0}
        self.counts = {"ready": 3, "chunked": 2, "unchunked": 1, "indexed": 7, "present": 4, "seconds": 5}
        self.dense_counts = {"dense_total": 11, "dense_scope": 11, "dense_present": 3,
                             "dense_copied": 2, "dense_embedded": 6, "dense_tokens": 120,
                             "dense_requests": 2, "dense_seconds": 20, "dense_points": 11,
                             "dense_indexed": 11, "dense_limit": 0}
        self.dense_progress: Callable[[int], None] | None = None
        self.dense_polls = 0
        self.generation_reads = 0
        self.fail_phase: str | None = None
        self.interrupt_phase: str | None = None
        self.statements: list[str] = []
        self.statement_directories: list[tuple[str, str]] = []
        self.reports: list[dict[str, Any]] = []
        self.report_write_code = 0
        self.treatment_reads: list[dict[str, Any]] = []
        self.treatment_payloads: list[dict[str, Any]] = []
        self.treatment_results: list[treatment.Treatment] = []
        self.treatment_distribution = test_index_treatment.treatment_for()
        self.treatment_distribution = replace(
            self.treatment_distribution,
            by_source={"pattern": sum(self.treatment_distribution.by_source.values())},
        )
        self.executions: list[tuple[list[str], str | None, str | None]] = []
        self.engine_holder: str | None = None
        self.engine_releases = 0
        self.lock_paths: list[str] = []
        self.identity_checked = False
        self.sibling_lockfiles: dict[str, dict[str, Any]] = {}
        self.manifest_hashes: dict[str, str] = {}
        self.manifest_rows: tuple[tuple[str, str, str], ...] = ()
        self.smoke_row: dict[str, object] | None = {
            "run_id": str(UUID(int=1)), "stack": "ci", "kind": "smoke",
            "finished_at": NOW.isoformat(),
        }

    def seed_finished_report(self, row: GenerationRow, courts: tuple[str, ...]) -> dict[str, Any]:
        """Record a completed fictitious build without running its stages."""

        assert row.built_at is not None
        seconds = int((datetime.fromisoformat(row.built_at) - datetime.fromisoformat(row.created_at)).total_seconds())
        completed = buildreport.BuildReport(
            row.gen_id, (NOW + timedelta(microseconds=len(self.reports) + 1)).isoformat(),
            row.state, None, row.failure_reason, 0, {}, {}, {"accounted": {
                court: buildreport.Accounted(0, 0, 0, 0, True) for court in courts
            }},
            buildreport.WallClock(
                0, None, None, row.created_at, row.lexical_built_at,
                row.dense_built_at, row.built_at, seconds, False,
            ), replace(self.treatment_distribution, materialized_at=row.built_at, treatment_snapshot=None),
        ).to_json()
        self.reports.append(completed)
        return completed

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.chown_calls.append((Path(path), uid, gid))

    def write_bytes(self, path: PathLike, data: bytes, *, mode: int = 0o644) -> None:
        super().write_bytes(path, data, mode=mode)
        self.seed_writes.append(Path(path))

    def write_text(
        self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644,
    ) -> None:
        super().write_text(path, text, encoding=encoding, mode=mode)
        self.seed_writes.append(Path(path))

    def listdir(self, path: PathLike) -> list[str]:
        if self.unreadable_root and Path(path) == self.snapshots:
            raise OSError("unreadable snapshots root")
        return super().listdir(path)

    def rmtree(self, path: PathLike) -> None:
        if self.interrupt_rmtree and Path(path).parent == self.snapshots:
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

    def _write_agreement(self, job: int, args: dict[str, Any]) -> None:
        directory = staging.work_directory(
            args["label"], args["snapshot"][:-staging.DATE_SUFFIX_LENGTH],
            work_root=self.work,
        )
        court = args["court"]
        if self.agreement_failure is not None:
            (directory / f"{court}.{worker_identity.AGREEMENT_FAILURE_NAME}").write_text(
                json.dumps({
                    "schema": 1, "job": job, "court": court,
                    "reason": self.agreement_failure, "table": None, "error": None,
                    "at": NOW.isoformat(),
                })
            )
            return
        defaults = {
            "documents": 1, "gideon_pairs": 4, "map_rows": 5,
            "map_outside": 1, "map_pairs": 3, "agreed": 2,
            "gideon_only": 2, "map_only_seen": 1, "map_only_missed": 0,
        }
        (directory / f"{court}.{worker_identity.AGREEMENT_RECORD_NAME}").write_text(
            json.dumps({
                "schema": 1, "label": args["label"], "snapshot": args["snapshot"],
                "court": court, "job": job,
                **(self.agreement_figures.get(court) or defaults),
                "seconds": 0.5, "computed_at": NOW.isoformat(),
            })
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
        self.lock_paths.append(str(path))
        if str(path) == backuplock.ENGINE_LOCK.path:
            if self.engine_holder is not None:
                return self.engine_holder
            self.engine_holder = record
            return None
        assert str(path) == backuplock.CORPUS_LOCK.path
        if self.lock_holder is not None:
            return self.lock_holder
        self.lock_holder = record
        return None

    def release_lock(self, path: PathLike) -> None:
        if str(path) == backuplock.ENGINE_LOCK.path:
            self.engine_holder = None
            self.engine_releases += 1
            return
        assert str(path) == backuplock.CORPUS_LOCK.path
        self.lock_holder = None
        self.lock_releases += 1

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        if str(path) == SITE_PATH:
            if self.site_text is None:
                raise FileNotFoundError(str(path))
            return self.site_text
        if str(path) in {f"{RENDERED}/compose.yaml", f"{CI_ROOT}/compose.yaml"}:
            return yaml.safe_dump({"services": {name: {} for name in self.declared}})
        return super().read_text(path, encoding=encoding)

    def _index_statement(
        self, command: list[str], input: str,
    ) -> subprocess.CompletedProcess[str]:
        """Answer generation reads and clustering, lexical, and dense queue jobs."""

        self.statements.append(input)
        if "DO $generation$" in input:
            phase = "generation"
        elif guards.CHUNK_PROJECTION_SQL in input:
            phase = "guard"
        elif "WHERE state = 'live'" in input:
            phase = "reserve"
        elif "UPDATE public.index_generations" in input and ":'v_dense_bytes'" in input:
            phase = "footprint"
        elif "WHERE collection = :'v_collection'" in input:
            phase = "collections"
        elif "public.index_generations" in input:
            if self.engine_holder is not None and not self.identity_checked:
                phase = "generation-read"
                self.identity_checked = True
            else:
                phase = {worker_identity.DENSE_TASK: "dense-counts", worker_identity.DUPES_TASK: "cluster-counts"}.get(self.active_task, "counts")
        elif "SELECT id FROM procrastinate_jobs" in input:
            lock = self._bound(input, "v_lock")
            self.active_task = (worker_identity.DENSE_TASK if lock.startswith(worker_identity.DENSE_LOCK_PREFIX) else
                                worker_identity.DUPES_TASK if lock.startswith(worker_identity.DUPES_LOCK_PREFIX) else worker_identity.LEXICAL_TASK)
            phase = {worker_identity.DENSE_TASK: "dense-live-job", worker_identity.DUPES_TASK: "cluster-live-job"}.get(self.active_task, "live-job")
        elif "procrastinate_defer_jobs_v1" in input:
            self.active_task = self._bound(input, "v_task")
            phase = {worker_identity.DENSE_TASK: "dense-defer", worker_identity.DUPES_TASK: "cluster-defer"}.get(self.active_task, "defer")
        else:
            self.active_task = self.task_by_job[int(self._bound(input, "v_job_id"))]
            phase = {worker_identity.DENSE_TASK: "dense", worker_identity.DUPES_TASK: "cluster"}.get(self.active_task, "build")
        if self.interrupt_phase == phase:
            raise KeyboardInterrupt
        if self.fail_phase == phase:
            return subprocess.CompletedProcess(command, 3, "", "private diagnostic")
        if phase == "guard":
            return subprocess.CompletedProcess(command, 0, json.dumps(self.projection_counts), "")
        if phase == "reserve":
            return subprocess.CompletedProcess(command, 0, json.dumps([asdict(row) for row in self.promoted]), "")
        if phase == "footprint":
            name = self._bound(input, "v_name")
            generation = self.generations[name]
            lexical_bytes = generation.counts.get("lexical_bytes", generation.bytes)
            dense_bytes = int(self._bound(input, "v_dense_bytes"))
            footprint = {"lexical_bytes": lexical_bytes, "dense_bytes": dense_bytes,
                         "bytes": lexical_bytes + dense_bytes}
            self.generations[name] = replace(
                generation, counts={**generation.counts, "lexical_bytes": lexical_bytes, "dense_bytes": dense_bytes},
                bytes=footprint["bytes"],
            )
            self.footprints.append(footprint)
            return subprocess.CompletedProcess(command, 0, json.dumps(footprint), "")
        if phase == "generation":
            payload = json.loads(self._bound(input, "v_payload"))
            row = next((row for row in self.generations.values() if row.collection == payload["collection"] and row.inputs_hash == payload["inputs_hash"]), None)
            outcome = "found" if row is not None else "created"
            if row is None:
                row = GenerationRow(
                    payload["name"], payload["collection"], payload["label"], (payload["label"],),
                    payload["pipeline"], payload["embedding_space"], payload["inputs_hash"],
                    "building", None, self.counts, 1024, None, None, None, None, None, NOW.isoformat(), clustered_at=None,
                )
            elif payload["label"] not in row.lockfiles:
                row = replace(row, lockfiles=(*row.lockfiles, payload["label"]))
            self.generations[row.gen_id] = row
            notice = {"outcome": outcome, "name": row.gen_id, "state": row.state}
            return subprocess.CompletedProcess(command, 0, "", "NOTICE: INDEX_GENERATION:" + json.dumps(notice) + "\n")
        if phase in {"collections", "counts", "dense-counts", "cluster-counts", "generation-read"}:
            self.generation_reads += 1
            if phase == "collections":
                row = next((row for row in self.generations.values()
                            if row.collection == self._bound(input, "v_collection")
                            and row.inputs_hash == self._bound(input, "v_inputs_hash")), None)
            else:
                row = self.generations.get(self._bound(input, "v_name"))
            return subprocess.CompletedProcess(command, 0, json.dumps(asdict(row)) if row is not None else "", "")
        if phase in {"live-job", "dense-live-job", "cluster-live-job"}:
            live_id = (self.dense_live_id if self.active_task == worker_identity.DENSE_TASK else
                       self.cluster_live_id if self.active_task == worker_identity.DUPES_TASK else self.live_id)
            return subprocess.CompletedProcess(command, 0, str(live_id) if live_id is not None else "", "")
        if phase in {"defer", "dense-defer", "cluster-defer"}:
            args = json.loads(self._bound(input, "v_args"))
            self.next_job += 1
            self.jobs[self.next_job] = args
            self.task_by_job[self.next_job] = self.active_task
            self.deferred.append(args)
            if self.active_task == worker_identity.DENSE_TASK:
                self.dense_live_id = self.next_job
            elif self.active_task == worker_identity.DUPES_TASK:
                self.cluster_live_id = self.next_job
            else:
                self.live_id = self.next_job
            return subprocess.CompletedProcess(command, 0, str(self.next_job), "")
        job = int(self._bound(input, "v_job_id"))
        if self.active_task == worker_identity.DUPES_TASK:
            pending = self.cluster_pending_polls > 0
            self.cluster_pending_polls = max(0, self.cluster_pending_polls - 1)
            status = "doing" if pending else "failed" if self.cluster_failure_reason else "succeeded"
            name = self.jobs[job]["name"]
            row = self.generations[name]
            self.generations[name] = replace(
                row, counts={**row.counts, **self.cluster_counts},
                clustered_at=row.clustered_at if pending or self.cluster_failure_reason else self.cluster_finish_stamp,
                state="failed" if self.cluster_failure_reason and not pending else
                "building" if row.state == "failed" else row.state,
                failure_reason=self.cluster_failure_reason if not pending else None,
            )
            if not pending:
                self.cluster_live_id = None
            return subprocess.CompletedProcess(command, 0, f"{job}|{status}|1", "")
        if self.active_task == worker_identity.DENSE_TASK:
            self.dense_polls += 1
            if self.dense_progress is not None:
                self.dense_progress(self.dense_polls)
            pending = self.dense_pending_polls > 0
            self.dense_pending_polls = max(0, self.dense_pending_polls - 1)
            status = "doing" if pending else "failed" if self.dense_failure_reason else "succeeded"
            name = self.jobs[job]["name"]
            row = self.generations[name]
            self.generations[name] = replace(
                row, counts={**row.counts, **self.dense_counts},
                dense_built_at=row.dense_built_at if pending or self.dense_failure_reason else self.dense_finish_stamp,
                built_at=row.built_at if pending or self.dense_failure_reason or self.dense_finish_stamp is None else self.dense_finish_stamp,
                state=row.state if pending or row.state == "live" else "failed" if self.dense_failure_reason else
                "built" if self.dense_finish_stamp and row.lexical_built_at else "building",
                failure_reason=self.dense_failure_reason if not pending and row.state != "live" else None,
            )
            if not pending:
                self.dense_live_id = None
            return subprocess.CompletedProcess(command, 0, f"{job}|{status}|2", "")
        if self.pending_polls > 0:
            self.pending_polls -= 1
            status = "doing"
        else:
            status = "failed" if self.failure_reason else "succeeded"
            name = self.jobs[job]["name"]
            row = self.generations[name]
            self.generations[name] = replace(
                row, lexical_built_at=row.lexical_built_at if self.failure_reason and row.state == "live" else self.finish_stamp,
                state=row.state if row.state == "live" else "failed" if self.failure_reason else "built" if row.dense_built_at else "building",
                failure_reason=self.failure_reason if row.state != "live" else None,
            )
            self.live_id = None
        return subprocess.CompletedProcess(command, 0, f"{job}|{status}|1", "")

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        command = list(argv)
        self.calls.append(command)
        self.executions.append((command, input, self.engine_holder))
        if command == smoke.reader_argv(RENDERED):
            assert input is not None and "FROM public.eval_runs" in input
            self.statements.append(input)
            self.statement_directories.append((RENDERED, input))
            if self.interrupt_phase == "smoke":
                raise KeyboardInterrupt
            return subprocess.CompletedProcess(
                command, 3 if self.fail_phase == "smoke" else 0,
                json.dumps(self.smoke_row) if self.smoke_row is not None else "", "private diagnostic",
            )
        if command == list(reserve.DATA_DF_ARGV) or command[:2] == ["du", "-sb"]:
            phase = "df" if command[0] == "df" else "du"
            if self.interrupt_phase == phase:
                raise KeyboardInterrupt
            if phase == "du" and self.du_error is not None:
                raise self.du_error
            if phase == "df":
                text = self.df_text if self.df_text is not None else f"Size Avail\n8000000000 {self.free_bytes}\n"
            else:
                text = self.du_text if self.du_text is not None else f"{self.du_bytes}\t{command[-1]}\n"
            return subprocess.CompletedProcess(command, 3 if self.fail_phase == phase else 0, text, "private diagnostic")
        if command in [stack.compose_argv(directory, "ps", "--all", "--format", "json")
                       for directory in (RENDERED, CI_ROOT)]:
            self.ps_reads += 1
            if self.interrupt_phase == "preconditions":
                raise KeyboardInterrupt
            if self.ps_reads == self.state_read and self.store_state_error is not None:
                raise self.store_state_error
            text = json.dumps([{"Service": name, "State": "running", "Health": self.health[name]} for name in self.health])
            if self.ps_reads == self.state_read and self.store_state_text is not None:
                text = self.store_state_text
            return subprocess.CompletedProcess(command, self.store_state_code if self.ps_reads == self.state_read else 0, text, "private diagnostic")
        if command in (worker.psql_argv(RENDERED), worker.psql_argv(CI_ROOT)):
            assert input is not None
            statement_dir = RENDERED if command == worker.psql_argv(RENDERED) else str(CI_ROOT)
            self.statement_directories.append((statement_dir, input))
            if "DO $stage_counts$" in input or "FROM public.stage_counts" in input:
                self.statements.append(input)
                phase = "stage-counts-write" if "DO $stage_counts$" in input else "stage-counts-read"
                if self.interrupt_phase == phase:
                    raise KeyboardInterrupt
                if self.fail_phase == phase:
                    return subprocess.CompletedProcess(command, 3, "", "private diagnostic")
                if phase == "stage-counts-write":
                    stage_payload = json.loads(self._bound(input, "v_payload"))
                    pending: dict[tuple[str, str, str], dict[str, Any]] = {}
                    for counts in stage_payload["courts"]:
                        key = (stage_payload["label"], stage_payload["source"], counts["court"])
                        held_counts = self.stage_counts_rows.get(key)
                        if held_counts is not None:
                            if any(held_counts[field] != counts[field] for field in (
                                "dockets", "opinion_clusters", "citations", "opinions",
                            )):
                                return subprocess.CompletedProcess(
                                    command, 3, "", f"ERROR: STAGE_COUNTS_CONFLICT:{counts['court']}\n",
                                )
                        else:
                            pending[key] = {
                                "label": stage_payload["label"], "source": stage_payload["source"],
                                **counts, "staged_at": stage_payload["staged_at"],
                            }
                    self.stage_counts_rows.update(pending)
                    return subprocess.CompletedProcess(command, 0, json.dumps({"inserted": len(pending)}), "")
                counts_label = self._bound(input, "v_label")
                return subprocess.CompletedProcess(command, 0, json.dumps([
                    counts for (label, _, _), counts in sorted(self.stage_counts_rows.items())
                    if label == counts_label
                ]), "")
            if (
                "WITH RECURSIVE chain AS" in input
                or command == worker.psql_argv(CI_ROOT) and "DO $cut$" in input
                or "public.generation_documents" in input
            ):
                self.statements.append(input)
                phase = ("copy-read" if "WITH RECURSIVE chain AS" in input else "lockfile" if "DO $cut$" in input
                         else "treatment-manifest" if "canonical_text_sha256, doc_id" in input else "gold")
                if self.interrupt_phase == phase:
                    raise KeyboardInterrupt
                if self.fail_phase == phase:
                    return subprocess.CompletedProcess(command, 3, "", "private diagnostic")
                if phase == "copy-read":
                    label: str | None = self._bound(input, "v_label")
                    rows: list[dict[str, Any]] = []
                    while label is not None and label in self.record_rows:
                        cut = copy.deepcopy(self.record_rows[label])
                        cut["sources"] = [self.snapshot_rows[(pin["source"], pin["snapshot_date"])]
                                          for pin in cut["sources"]]
                        rows.append(cut)
                        label = cut["base"]
                    return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(row) for row in reversed(rows)), "")
                if phase == "lockfile":
                    copied = json.loads(self._bound(input, "v_payload"))
                    self.sibling_lockfiles.setdefault(copied["label"], copied)
                    return subprocess.CompletedProcess(command, 0, "", "")
                if phase == "treatment-manifest":
                    return subprocess.CompletedProcess(command, 0, "\n".join(
                        json.dumps({"source_id": source_id, "canonical_text_sha256": digest, "doc_id": doc_id})
                        for source_id, digest, doc_id in sorted(self.manifest_rows)
                    ), "")
                identifiers = json.loads(self._bound(input, "v_source_ids"))
                return subprocess.CompletedProcess(command, 0, "\n".join(
                    json.dumps({"source_id": identifier, "canonical_text_sha256": digest})
                    for identifier, digest in self.manifest_hashes.items() if identifier in identifiers
                ), "")
            if treatment.READ_SQL in input:
                self.statements.append(input)
                self.treatment_reads.append(json.loads(self._bound(input, "v_payload")))
                if self.interrupt_phase == "treatment-read":
                    raise KeyboardInterrupt
                if self.fail_phase == "treatment-read":
                    return subprocess.CompletedProcess(command, 3, "", "private diagnostic")
                observed = test_index_treatment.read_for(self.treatment_distribution)
                return subprocess.CompletedProcess(command, 0, json.dumps(observed.to_payload()), "")
            if treatment.WRITE_SQL in input:
                self.statements.append(input)
                treatment_payload = json.loads(self._bound(input, "v_payload"))
                self.treatment_payloads.append(treatment_payload)
                if self.interrupt_phase == "treatment":
                    raise KeyboardInterrupt
                if self.fail_phase == "treatment":
                    return subprocess.CompletedProcess(command, 3, "", "private diagnostic")
                treatment_snapshot = (
                    None if treatment_payload["treatment_source"] is None
                    else f"{treatment_payload['treatment_source']}-{treatment_payload['treatment_snapshot_date']}"
                )
                materialized = replace(
                    self.treatment_distribution,
                    materialized_at=(NOW + timedelta(microseconds=len(self.treatment_results) + 1)).isoformat(),
                    pattern_set=treatment_payload["pattern_set"], signal_pattern=treatment_payload["signal_pattern"],
                    signal_list=treatment_payload["signal_list"], treatment_snapshot=treatment_snapshot,
                    clusters=treatment_payload["clusters"],
                    by_state={
                        **{state: sum(row["state"] == state for row in treatment_payload["rows"])
                           for state in ("negative", "caution")},
                        "unchecked": treatment_payload["clusters"] - len(treatment_payload["rows"]),
                    },
                    by_source=treatment_payload["by_source"], by_signal=treatment_payload["by_signal"],
                    outside=treatment_payload["outside"], by_no_state_reason=treatment_payload["by_no_state_reason"],
                )
                self.treatment_results.append(materialized)
                return subprocess.CompletedProcess(command, 0, json.dumps(materialized.to_json()), "")
            if "public.build_reports" in input:
                self.statements.append(input)
                if self.interrupt_phase == "report":
                    raise KeyboardInterrupt
                if "INSERT INTO public.build_reports" in input:
                    if self.report_write_code:
                        return subprocess.CompletedProcess(command, self.report_write_code, "", "private diagnostic")
                    report_row = json.loads(self._bound(input, "v_payload"))
                    report_row["written_at"] = (NOW + timedelta(microseconds=len(self.reports) + 1)).isoformat()
                    self.reports.append(report_row)
                    return subprocess.CompletedProcess(command, 0, json.dumps(report_row), "")
                name = self._bound(input, "v_name")
                newest = next((row for row in reversed(self.reports) if row["gen_id"] == name), None)
                return subprocess.CompletedProcess(command, 0, json.dumps(newest) if newest is not None else "", "")
            if (
                "public.index_generations" in input or guards.CHUNK_PROJECTION_SQL in input
                or "SELECT id FROM procrastinate_jobs" in input
                or "procrastinate_defer_jobs_v1" in input
                and self._bound(input, "v_task") in {worker_identity.LEXICAL_TASK, worker_identity.DENSE_TASK, worker_identity.DUPES_TASK}
                or "SELECT id, status, attempts" in input
                and int(self._bound(input, "v_job_id")) in self.jobs
            ):
                return self._index_statement(command, input)
            if "DO $installed$" in input:
                installed_payload = json.loads(self._bound(input, "v_payload"))
                label = installed_payload["label"]
                held = self.record_rows.get(label)
                tag = (
                    "MISSING" if held is None else
                    "SUPERSEDED" if held["state"] == "superseded" else
                    "NOT_INSTALLING" if held["state"] == "cut" else None
                )
                if tag is not None:
                    return subprocess.CompletedProcess(command, 3, "", f"ERROR: CORPUS_LOCKFILE_{tag}:{label}\n")
                assert held is not None
                if held["state"] == "installing":
                    held.update(state="installed", installed_at=installed_payload["installed_at"])
                superseded = []
                for other_label, row in sorted(self.record_rows.items()):
                    if other_label < label and row["state"] in {"installing", "installed"}:
                        row["state"] = "superseded"
                        superseded.append(other_label)
                notice = {key: held[key] for key in ("label", "state", "installed_at")}
                notice["superseded"] = superseded
                return subprocess.CompletedProcess(command, 0, "", "NOTICE: CORPUS_INSTALLED:" + json.dumps(notice) + "\n")
            if "procrastinate_defer_jobs_v1" in input:
                args = json.loads(self._bound(input, "v_args"))
                self.next_job += 1
                job = self.next_job
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
                elif self._bound(input, "v_task") == worker_identity.AGREEMENT_TASK:
                    self.agreement_jobs[job] = args
                    self.agreement_deferred.append(args)
                    self.agreement_events.append(("defer", args["court"]))
                    self._write_agreement(job, args)
                elif self._bound(input, "v_task") == worker_identity.OVERRULED_TASK:
                    self.overruled_jobs[job] = args
                    self.overruled_deferred.append(args)
                    self.overruled_events.append("defer")
                    directory = self.work / args["label"] / "treatment"
                    directory.mkdir(exist_ok=True)
                    if self.overruled_failure is not None:
                        (directory / worker_identity.OVERRULED_FAILURE_NAME).write_text(json.dumps({
                            "schema": 1, "job": job, "court": args["court"],
                            "reason": self.overruled_failure, "table": None, "error": None,
                            "at": NOW.isoformat(),
                        }))
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
                if self.interrupt_agreement_job and job in self.agreement_jobs:
                    raise KeyboardInterrupt
                if self.interrupt_overruled_job and job in self.overruled_jobs:
                    raise KeyboardInterrupt
                assert (job in self.job_destinations or job in self.stage_jobs
                        or job in self.caselaw_jobs or job in self.agreement_jobs or job in self.overruled_jobs)
                if job in self.caselaw_jobs:
                    self.caselaw_events.append(("read", self.caselaw_jobs[job]["court"]))
                if job in self.agreement_jobs:
                    self.agreement_events.append(("read", self.agreement_jobs[job]["court"]))
                if job in self.overruled_jobs:
                    self.overruled_events.append("read")
                polls = self.job_polls[job]
                self.job_polls[job] += 1
                status = (
                    "doing" if polls == 0 else
                    "failed" if (job in self.stage_jobs and self.stage_failure is not None)
                    or (job in self.caselaw_jobs and self.caselaw_failure is not None)
                    or (job in self.agreement_jobs and self.agreement_failure is not None)
                    or (job in self.overruled_jobs and self.overruled_failure is not None)
                    else "succeeded"
                )
                return subprocess.CompletedProcess(command, 0, f"{job}|{status}|1\n", "")
            if "FROM public.known_overruled" in input:
                self.overruled_events.append("counts")
                self.overruled_count_reads.append({
                    name: self._bound(input, f"v_{name}") for name in (
                        "source", "snapshot_date", "caselaw_source", "caselaw_snapshot_date", "court",
                    )
                })
                return subprocess.CompletedProcess(command, 0, json.dumps(self.overruled_counts) + "\n", "")
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
            if "FROM public.chunks AS c" in input:
                court = self._bound(input, "v_court")
                self._bound(input, "v_pipeline")
                self._bound(input, "v_maximum")
                self._bound(input, "v_minimum")
                if court in self.caselaw_chunk_counts:
                    counts = self.caselaw_chunk_counts[court]
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
                        "ready": ready, "chunked": ready, "chunks": ready,
                        "by_section_type": {"majority": ready} if ready else {},
                        "token_minimum": 100 if ready else None,
                        "token_median": 100.0 if ready else None,
                        "token_p95": 100.0 if ready else None,
                        "token_maximum": 100 if ready else None,
                        "over_maximum": 0, "under_minimum": 0,
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
                    self.caselaw_counts[court] = counts
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
                for pin in payload["sources"]:
                    snapshot = self.snapshot_rows.get((pin["source"], pin["snapshot_date"]))
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
                for pin in payload["sources"]:
                    snapshot_key = (pin["source"], pin["snapshot_date"])
                    snapshot = self.snapshot_rows.get(snapshot_key)
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
        (self.checkout / "models.lock").write_text(models_text())
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
        self._enable_stage()

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
            hashlib.sha256(sidecar.encode()).hexdigest(), 1, len(data), (index,), (entry,),
            self.source.first_courts if self.source.carries_courts else None,
        )
        lockfile = Lockfile(
            1, label, worker_identity.PIPELINE_VERSION,
            "2099-01-03T04:05:06Z", "tranche", {"example": pin},
        )
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
        result = load_lockfile(path, known_sources={self.source.name: self.source})
        assert result.lockfile is not None
        lockfile = result.lockfile
        pin = lockfile.sources["example"]
        path.write_text(render_lockfile(replace(
            lockfile, sources={"example": replace(pin, mirror_url=self.mirror_url)},
        )))

    def _hold_record(self, state: str, label: str | None = None) -> None:
        path = self.checkout / "corpus/lockfiles" / f"{label or self.labels[0]}.yaml"
        loaded = load_lockfile(path, known_sources={self.source.name: self.source})
        assert loaded.lockfile is not None
        lockfile = loaded.lockfile
        pin = lockfile.sources["example"]
        self.host.record_rows[lockfile.label] = {
            "label": lockfile.label, "schema": lockfile.schema,
            "pipeline": lockfile.pipeline, "cut_at": lockfile.cut_at,
            "reason": lockfile.reason, "base": lockfile.base,
            "installed_at": NOW.isoformat() if state == "installed" else None, "state": state,
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
        self._enable_stage()
        return label

    def _enable_stage(self, *, missing: str | None = None) -> None:
        """Give each committed fixture a court pin and dated stage inputs."""

        for label in self.labels:
            lock_path = self.checkout / "corpus/lockfiles" / f"{label}.yaml"
            loaded = load_lockfile(lock_path, known_sources={self.source.name: self.source})
            assert loaded.lockfile is not None
            lockfile = loaded.lockfile
            pin = lockfile.sources["example"]
            entries = {entry.path: entry for entry in pin.entries}
            for table in (*worker_identity.STAGE_TABLES, worker_identity.AGREEMENT_TABLE):
                name = data_file(table, pin.snapshot_date)
                destination = fetch.snapshot_destination("example", pin.snapshot_date, name)
                path = self.snapshots / destination
                if label == self.labels[0] and table == missing:
                    entries.pop(name, None)
                    path.unlink(missing_ok=True)
                    path.with_name(path.name + worker_identity.RECORD_SUFFIX).unlink(missing_ok=True)
                    continue
                body = f"fictitious {table} rows for {label}\n".encode()
                entry = SidecarEntry(name, hashlib.sha256(body).hexdigest(), len(body))
                entries[name] = entry
                path.write_bytes(body)
                url = f"{self.source.base_url}{name}"
                (path.with_name(path.name + worker_identity.RECORD_SUFFIX)).write_text(
                    json.dumps(_whole_record(body, url, 1))
                )
            sidecar = render_sidecar(tuple(entries.values()))
            (lock_path.parent / label / "example.sha256").write_text(sidecar)
            updated = replace(
                pin, sidecar_sha256=hashlib.sha256(sidecar.encode()).hexdigest(),
                files=len(entries), bytes=sum(entry.size for entry in entries.values()),
                entries=tuple(sorted(entries.values(), key=lambda entry: entry.path)),
                courts=("ca6", "scotus"),
            )
            lock_path.write_text(render_lockfile(replace(
                lockfile, sources={"example": updated},
            )))
        self.source = replace(
            self.source, carries_courts=True, first_courts=("ca6", "scotus"),
        )

    def seed_report_work(self, label: str) -> None:
        """Seed recorded counts and the stage and agreement work files."""

        self._seed_complete_stage(label)
        record = staging.read_record(self.host, label, self.source.name, work_root=self.work)
        assert isinstance(record, staging.StageRecord)
        for court in record.courts:
            self.host.stage_counts_rows[(label, record.source, court)] = {
                "label": label, "source": record.source, "court": court,
                **{table.replace("-", "_"): record.counts[court][table]
                   for table in worker_identity.STAGE_TABLES[1:]},
                "staged_at": record.staged_at,
            }
            self.host._write_agreement(92, {"label": label, "snapshot": record.snapshot, "court": court})

    def _seed_complete_stage(self, label: str | None = None) -> Path:
        label = label or self.labels[0]
        lock_path = self.checkout / "corpus/lockfiles" / f"{label}.yaml"
        loaded = load_lockfile(lock_path, known_sources={self.source.name: self.source})
        assert loaded.lockfile is not None
        pin = loaded.lockfile.sources[self.source.name]
        entries = {entry.path: entry for entry in pin.entries}
        args = {
            "label": label, "snapshot": f"{self.source.name}-{pin.snapshot_date}",
            "courts": list(pin.courts or ()),
            "inputs": {
                table: {"path": data_file(table, pin.snapshot_date),
                        "sha256": entries[data_file(table, pin.snapshot_date)].sha256}
                for table in worker_identity.STAGE_TABLES
            },
        }
        self.host._write_stage(91, args)
        return staging.work_directory(label, self.source.name, work_root=self.work)

    def _add_captured_source(self) -> tuple[SourceDefinition, Path, Path]:
        """Add one visibly fictitious saved page to the two install fixtures."""
        source = SourceDefinition(
            "treatment", "https://capture.example.invalid/table", False, (),
            lambda: (), lambda _: report.Problem("Fictitious captured source", "Use its saved page."),
            captured_file="decisions-overruled.html",
        )
        data = b"<!doctype html><title>Fictitious saved install page</title>\n"
        entry = SidecarEntry("decisions-overruled.html", hashlib.sha256(data).hexdigest(), len(data))
        sidecar = render_sidecar((entry,))
        pin = SourcePin(
            "2099-01-04", source.base_url, None,
            hashlib.sha256(sidecar.encode()).hexdigest(), 1, len(data),
            (IndexDocument(entry.path, source.base_url, entry.sha256, entry.size),),
            (entry,), captured=True,
        )
        directory = self.checkout / "corpus/lockfiles"
        for label in self.labels:
            loaded = load_lockfile(directory / f"{label}.yaml", known_sources={
                self.source.name: self.source,
            })
            assert loaded.lockfile is not None
            (directory / label / f"{source.name}.sha256").write_text(sidecar)
            (directory / label / f"{source.name}.{entry.path}").write_bytes(data)
            (directory / f"{label}.yaml").write_text(render_lockfile(replace(
                loaded.lockfile, sources={**loaded.lockfile.sources, source.name: pin},
            )))
        companion = directory / self.labels[0] / f"{source.name}.{entry.path}"
        target = self.snapshots / fetch.snapshot_destination(source.name, pin.snapshot_date, entry.path)
        return source, companion, target

    def run_install(
        self, label: str | None = None, *, sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        sources: tuple[SourceDefinition, ...] | None = None,
        clock: Callable[[], datetime] = lambda: NOW,
    ) -> tuple[int, str, str]:
        self.host.current_label = label or self.labels[0]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = install.run_corpus_install(
                argparse.Namespace(label=label or self.labels[0]),
                host=self.host, rendered_dir=RENDERED, checkout=self.checkout,
                snapshots_root=self.snapshots, work_root=self.work, clock=clock,
                sleep=sleep or (lambda _seconds: None),
                monotonic=monotonic or (lambda: 0.0),
                sources=sources if sources is not None else (self.source,),
            )
        return code, out.getvalue(), err.getvalue()

    def _assert_removal_fix(self, out: str) -> None:
        self.assertIn(f"remove {self.file} and {self.record_file}", out.lower())
        self.assertIn(report.command("corpus install"), out)

    def _lockfile(self) -> Lockfile:
        loaded = load_lockfile(
            self.checkout / "corpus/lockfiles" / f"{self.labels[0]}.yaml",
            known_sources={self.source.name: self.source},
        )
        assert loaded.lockfile is not None
        return loaded.lockfile

    def _assert_build_rows(self, out: str) -> None:
        lockfile = self._lockfile()
        model_lock = models.load_models_lock(self.checkout / "models.lock")
        assert model_lock.lock is not None
        profile = models.select_profile(model_lock.lock, model_lock.lock.reference)
        assert isinstance(profile, models.HardwareProfile) and profile.embedding_space is not None
        space = profile.embedding_space.id
        collection = generations.COLLECTIONS[0].name
        name = generations.generation_name(collection, lockfile.label, space)
        inputs_hash = generations.inputs_hash(lockfile, collection, space, sources=(self.source,))
        row = self.host.generations[name]
        counts = row.counts
        cluster_job = next(job for job, task in self.host.task_by_job.items() if task == worker_identity.DUPES_TASK)
        build_report = next(item for item in self.host.reports if item["gen_id"] == name)
        materialized = self.host.treatment_results[-1]
        states = ", ".join(f"{state} {materialized.by_state[state]}" for state in treatment.RISK_STATES)
        courts = tuple(build_report["courts"].values())
        opinions = sum(court["opinions"] for court in courts)
        ready = sum(court["by_status"].get("ready", 0) for court in courts)
        failed = sum(court["by_status"].get("failed", 0) for court in courts)
        anchored = sum(court["anchors"]["anchored"] for court in courts)
        figures = sum(court["agreement"] is not None for court in courts)
        accounted = tuple(build_report["gates"]["accounted"].values())
        staged = sum(court["staged"] is not None for court in accounted)
        passed = sum(court["pass"] is True for court in accounted)
        lexical_job = next(job for job, task in self.host.task_by_job.items() if task == worker_identity.LEXICAL_TASK)
        dense_job = next(job for job, task in self.host.task_by_job.items() if task == worker_identity.DENSE_TASK)
        present, copied, embedded = (counts[f"dense_{key}"] for key in ("present", "copied", "embedded"))
        ready_documents, chunked_documents, chunks = (
            self.host.projection_counts[key] for key in ("ready", "chunked", "chunks")
        )
        pin = lockfile.sources[self.source.name]
        self.assertIn(
            f"preconditions: ok — {lockfile.label}: {self.source.name} {pin.snapshot_date}; "
            f"not yet recorded; embedding space {space}", out,
        )
        lines = out.splitlines()
        tail = lines[next(index for index, line in enumerate(lines) if line.startswith("collections:")):]
        evidence = self.host.smoke_row
        assert evidence is not None
        self.assertEqual(tail, [
            f"collections: ok — {collection}: needs build; generation {name}, inputs hash {inputs_hash}",
            "collections: ok — 1 of 1 need a build",
            f"smoke: ok — lockfile {lockfile.label}, pipeline {lockfile.pipeline}, embedding space {space}: "
            f"run {evidence['run_id']} ({evidence['stack']}, {evidence['kind']}, finished {NOW:%Y-%m-%dT%H:%M}Z)",
            f"generation: ok — {name}: building, created",
            f"guard: ok — {name}: ready {ready_documents}, chunked {chunked_documents}, chunks {chunks}, "
            f"projected chunks {-(-chunks * ready_documents // chunked_documents)}; "
            f"build refusal {profile.chunk_guards.build_refusal_chunks}, "
            f"serving ceiling {profile.chunk_guards.serving_ceiling_chunks}",
            f"reserve: ok — skipped: no promoted generation for {collection}; reserve not judged; "
            f"/data free {self.host.free_bytes} bytes",
            f"cluster: ok — {name}: deferred job {cluster_job}",
            f"cluster: ok — {name}: clusters {counts['cluster_clusters']} over "
            f"{counts['cluster_grouped']} chunks of {counts['cluster_chunks']} "
            f"({counts['cluster_members']} near-duplicate texts of {counts['cluster_texts']}), "
            f"boilerplate {counts['cluster_boilerplate']} clusters over {counts['cluster_flagged']} chunks "
            f"({100 * counts['cluster_flagged'] / counts['cluster_chunks']:.1f} % of {counts['cluster_chunks']}); "
            f"{counts['cluster_seconds']} s of the {worker_identity.CLUSTER_BUDGET_SECONDS} s budget; "
            f"clustered_at {row.clustered_at}",
            f"build: ok — {name}: deferred job {lexical_job}",
            f"build: ok — {name}: job {lexical_job} succeeded",
            f"counts: ok — {name}: documents ready {counts['ready']}, chunked {counts['chunked']}, "
            f"unchunked {counts['unchunked']}; chunks indexed {counts['indexed']}, "
            f"present {counts['present']}, leveled {counts.get('leveled', 0)}, total {counts['indexed'] + counts['present']}; "
            f"bytes {counts['lexical_bytes']}; lexical_built_at {row.lexical_built_at}; state building",
            f"dense: ok — {name}: deferred job {dense_job}",
            f"dense: ok — {name}: job {dense_job} succeeded, attempts 2",
            f"built: ok — {name}: points {counts['dense_points']}, indexed {counts['dense_indexed']}, "
            f"written {copied + embedded} (present {present}, leveled {counts.get('dense_leveled', 0)}, copied {copied}, embedded {embedded}), "
            f"tokens {counts['dense_tokens']}, requests {counts['dense_requests']}, "
            f"seconds {counts['dense_seconds']}; dense_built_at {row.dense_built_at}; state built; "
            f"bytes lexical {counts['lexical_bytes']}, dense {counts['dense_bytes']}, total {row.bytes}",
            f"treatment: ok — {name}: {treatment.summary(materialized)}",
            f"report: ok — {name}: written_at {build_report['written_at']}; courts {len(courts)}; "
            f"opinions {opinions}, ready {ready}, failed {failed}; anchored {anchored} of {ready}; "
            f"agreement figures {figures} of {len(courts)} courts; accounted {passed} of {staged} staged; "
            f"cluster {counts['cluster_seconds']} of {worker_identity.CLUSTER_BUDGET_SECONDS} s, pass; "
            f"seconds {build_report['seconds']}; treatment {states}",
            f"installed: ok — {lockfile.label}: installed at {NOW.isoformat()}; none superseded",
            f"clean: ok — removed {self.work / lockfile.label}",
            f"retain: ok — 1 kept ({lockfile.label}), 0 removed, 1 unpinned",
            f"Next: run {report.command(f'index promote {name}')} to serve it.",
        ])

    def test_smoke_refusal_leaves_the_label_installing_without_a_generation(self) -> None:
        """The install refuses a collection build without its sample's smoke."""

        self.host.smoke_row = None
        lockfile = self._lockfile()
        loaded = models.load_models_lock(self.checkout / "models.lock")
        assert loaded.lock is not None
        profile = models.select_profile(loaded.lock, loaded.lock.reference)
        assert isinstance(profile, models.HardwareProfile) and profile.embedding_space is not None
        name = generations.generation_name(generations.COLLECTIONS[0].name, lockfile.label, profile.embedding_space.id)
        sample = sample_name(name)
        code, out, err = self.run_install()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn(
            f"smoke: refuse — no passing smoke run for lockfile {lockfile.label}, pipeline {lockfile.pipeline}, "
            f"embedding space {profile.embedding_space.id}: none names generation {sample}", out,
        )
        self.assertIn(report.command(f"index build caselaw --sample --lockfile {lockfile.label}"), out)
        self.assertIn(report.command(
            f"eval run --slice smoke --stack ci --kind smoke --generation {sample}",
        ), out)
        self.assertIn(f"then retry {report.command('corpus install')}", out)
        stages = [line.split(":", 1)[0] for line in out.splitlines()]
        self.assertEqual(stages[-1], "smoke")
        self.assertNotIn("generation", stages)
        self.assertNotIn("installed", stages)
        self.assertEqual(self.host.record_rows[lockfile.label]["state"], "installing")
        self.assertEqual((self.host.generations, self.host.deferred, self.host.reports), ({}, [], []))
        self.assertFalse(any("DO $generation$" in sql for sql in self.host.statements))
        self.assertIsNone(self.host.lock_holder)
        self.assertEqual(self.host.lock_releases, 1)

        directory = self.checkout / "eval/reference/smoke"
        directory.mkdir(parents=True)
        reference = directory / "v1000.0.0.json"
        reference.write_text(json.dumps({
            "format": 2, "slice": "smoke", "corpus_lockfile": lockfile.label,
            "generation_id": sample, "verdict": "pass", "tag": reference.stem,
        }))
        read_count = self.host.calls.count(smoke.reader_argv(RENDERED))
        code, out, err = self.run_install()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(
            f"smoke: ok — lockfile {lockfile.label}, pipeline {lockfile.pipeline}, "
            f"embedding space {profile.embedding_space.id}: reference {reference.stem} ({reference.name})", out,
        )
        self.assertLess(out.index("smoke: ok"), out.index("generation: ok"))
        self.assertEqual(self.host.calls.count(smoke.reader_argv(RENDERED)), read_count)
        self.assertEqual(self.host.record_rows[lockfile.label]["state"], "installed")
        self.assertIsNone(self.host.lock_holder)
        self.assertEqual(self.host.lock_releases, 2)

    def test_unchanged_and_unfinished_collections_need_no_new_smoke_evidence(self) -> None:
        """A completed build's treatment and report can finish without a new gate."""

        code, out, err = self.run_install()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual([line.split(":", 1)[0] for line in out.splitlines()].count("smoke"), 1)
        statements = self.host.statements
        read = next(index for index, sql in enumerate(statements) if "FROM public.eval_runs" in sql)
        write = next(index for index, sql in enumerate(statements) if "DO $generation$" in sql)
        self.assertLess(read, write)
        self.host.smoke_row = None
        deferred = copy.deepcopy(self.host.deferred)
        for unfinished in (False, True):
            with self.subTest(unfinished=unfinished):
                if unfinished:
                    self.host.reports.clear()
                    self.seed_report_work(self.labels[0])
                call_count = len(self.host.calls)
                code, out, err = self.run_install()
                self.assertEqual((code, err), (0, ""), out)
                self.assertIn("collections: ok — 0 of 1 need a build", out)
                stages = [line.split(":", 1)[0] for line in out.splitlines()]
                self.assertNotIn("smoke", stages)
                self.assertNotIn(smoke.reader_argv(RENDERED), self.host.calls[call_count:])
                self.assertEqual("treatment" in stages, unfinished)
                self.assertEqual("report" in stages, unfinished)
                self.assertEqual(self.host.deferred, deferred)
                self.assertIsNone(self.host.lock_holder)

    def test_captured_copy_seeded_verified_recorded_and_retained(self) -> None:
        self._enable_stage()
        source, companion, target = self._add_captured_source()
        code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 0, out + err)
        data = companion.read_bytes()
        self.assertEqual(target.read_bytes(), data)
        self.assertIn(f"fetch: ok — {source.name}: 1 files, {len(data)} bytes; no fetches deferred", out)
        self.assertIn(f"{source.name} 2099-01-04", out)
        record_path = target.with_name(target.name + worker_identity.RECORD_SUFFIX)
        seeded = json.loads(record_path.read_text())
        self.assertEqual((seeded["form"], seeded["job"]), (worker_identity.SEEDED_FORM, None))
        self.assertEqual((seeded["host"], seeded["path"]),
                         (urlsplit(source.base_url).hostname, urlsplit(source.base_url).path))
        self.assertEqual((seeded["size"], seeded["sha256"]),
                         (len(data), hashlib.sha256(data).hexdigest()))
        self.assertEqual(self.host.seed_writes, [target, record_path])
        self.assertEqual(self.host.deferred_urls, [])
        self.assertIn(["sha256sum", str(target)], self.host.calls)
        self.assertIn("files match fetch records and pinned sidecars", out)
        self.assertEqual(self.host.snapshot_rows[(source.name, "2099-01-04")]["fetched_at"],
                         seeded["fetched_at"])
        self.assertEqual(self.host.record_rows[self.labels[0]]["sources"], [
            {"source": self.source.name, "snapshot_date": "2099-01-02"},
            {"source": source.name, "snapshot_date": "2099-01-04"},
        ])
        for args in (*self.host.stage_deferred, *self.host.caselaw_deferred,
                     *self.host.agreement_deferred):
            self.assertTrue(args["snapshot"].startswith(f"{self.source.name}-"))
        self.assertEqual(len(self.host.stage_deferred), 1)
        self.assertEqual(len(self.host.caselaw_deferred), len(self.source.first_courts))
        self.assertEqual(len(self.host.agreement_deferred), len(self.source.first_courts))
        self.assertEqual(self.host.overruled_events, ["defer", "read", "read", "counts"])
        self.assertEqual(len(self.host.overruled_deferred), 1)
        self.assertFalse((self.work / self.labels[0] / source.name / worker_identity.STAGE_RECORD_NAME).exists())
        self.assertIn("overruled: ok — treatment: 4 table rows, 6 listed decisions", out)
        self.assertIn("overruled: ok — treatment: 6 rows over 3 clusters, 0 seconds", out)
        self.assertTrue(target.parent.is_dir())
        self.assertNotIn(target.parent, self.host.removed_dirs)
        self.assertIn(f"retain: ok — 2 kept ({self.labels[0]})", out)

    def test_install_keeps_an_equal_captured_snapshot_without_seed_writes(self) -> None:
        self._enable_stage()
        source, companion, target = self._add_captured_source()
        self.assertEqual(self.run_install(sources=(self.source, source))[0], 0)
        record_path = target.with_name(target.name + worker_identity.RECORD_SUFFIX)
        before = record_path.read_bytes()
        self.host.seed_writes.clear()
        self.host.chown_calls.clear()
        code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(target.read_bytes(), companion.read_bytes())
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(self.host.seed_writes, [])
        self.assertEqual(self.host.chown_calls, [])
        self.assertEqual(self.host.deferred_urls, [])
        self.assertEqual(len(self.host.overruled_deferred), 1)
        self.assertIn(f"overruled: ok — {self.labels[0]} is installed; nothing to do", out)

    def test_unreadable_captured_copy_refuses_at_fetch(self) -> None:
        source, companion, target = self._add_captured_source()
        read_bytes = self.host.read_bytes
        reads = 0

        def unreadable_on_fetch(path: PathLike) -> bytes:
            nonlocal reads
            if Path(path) == companion:
                reads += 1
                if reads == 2:
                    raise OSError("fictitious unreadable committed page")
            return read_bytes(path)

        with patch.object(self.host, "read_bytes", side_effect=unreadable_on_fetch):
            code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 1, out + err)
        self.assertIn("preconditions: ok", out)
        self.assertIn("fetch: refuse", out)
        self.assertIn(str(companion), out)
        self.assertIn("Restore the lockfile and its companion files from the release checkout", out)
        self.assertFalse(target.exists())
        self.assertEqual(self.host.seed_writes, [])
        self.assertEqual(self.host.deferred_urls, [])
        self.assertEqual(self.host.record_writes, 0)

    def test_different_captured_snapshot_refuses_with_removal_fix(self) -> None:
        source, _, target = self._add_captured_source()
        other = b"A different visibly fictitious saved page.\n"
        target.parent.mkdir(parents=True)
        target.write_bytes(other)
        record_path = target.with_name(target.name + worker_identity.RECORD_SUFFIX)
        record_path.write_text(json.dumps(dict(
            _whole_record(other, source.base_url, 1), form=worker_identity.SEEDED_FORM,
            job=None, seconds=0.0,
        )))
        before = record_path.read_bytes()
        code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 1, out + err)
        self.assertIn("fetch: refuse", out)
        self.assertIn("different bytes", out)
        self.assertIn(f"Remove {target} and {record_path}", out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(target.read_bytes(), other)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(self.host.seed_writes, [])
        self.assertEqual(self.host.deferred_urls, [])
        self.assertEqual(self.host.record_writes, 0)

    def test_ok_path_keeps_existing_file(self) -> None:
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertIn("preconditions: ok", out)
        self.assertIn(f"{self.labels[0]}: example 2099-01-02; not yet recorded", out)
        pin = self._lockfile().sources[self.source.name]
        self.assertIn(f"fetch: ok — example: {pin.files} files, {pin.bytes} bytes; no fetches deferred", out)
        self.assertIn(f"verify: ok — {pin.files} files match fetch records and pinned sidecars", out)
        self.assertIn(f"record: ok — {self.labels[0]}: installing", out)
        self.assertIn(
            f"retain: ok — 1 kept ({self.labels[0]}), 0 removed, 1 unpinned", out,
        )
        self._assert_build_rows(out)
        self.assertEqual(self.host.record_rows[self.labels[0]]["state"], "installed")
        self.assertEqual(self.host.record_rows[self.labels[0]]["installed_at"], NOW.isoformat())
        self.assertEqual(self.host.lock_releases, 1)

    def test_overruled_rows_follow_ingest_and_bind_both_snapshots(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        source, companion, _ = self._add_captured_source()
        code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertLess(out.index("ingest: ok"), out.index("overruled: ok"))
        self.assertLess(out.index("overruled: ok"), out.index("agreement: ok"))
        self.assertLess(out.index("agreement: ok"), out.index("retain: ok"))
        self.assertEqual([line for line in out.splitlines() if line.startswith("overruled:")], [
            "overruled: ok — treatment: 4 table rows, 6 listed decisions; "
            "overruled resolved 4 (cite 2, name 2), unresolved 2 (ambiguous 1, absent 1); "
            "overruling resolved 4 (cite 3, docket 1), unresolved 2",
            "overruled: ok — treatment: 6 rows over 3 clusters, 0 seconds",
        ])
        self.assertEqual(self.host.overruled_deferred, [{
            "label": self.labels[0], "snapshot": "treatment-2099-01-04",
            "caselaw_snapshot": "example-2099-01-02", "court": "scotus",
            "input": {"path": "decisions-overruled.html", "sha256": hashlib.sha256(companion.read_bytes()).hexdigest()},
        }])
        self.assertEqual(self.host.overruled_count_reads, [{
            "source": "treatment", "snapshot_date": "2099-01-04", "caselaw_source": "example",
            "caselaw_snapshot_date": "2099-01-02", "court": "scotus",
        }])
        self.assertEqual(self.host.overruled_events, ["defer", "read", "read", "counts"])

    def test_overruled_wait_uses_the_shared_heartbeat(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        source, _, _ = self._add_captured_source()
        now = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += snapshots.HEARTBEAT_SECONDS

        code, out, err = self.run_install(sources=(self.source, source), sleep=sleep, monotonic=lambda: now[0])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sleeps, [1.0, 1.0, 1.0])
        self.assertIn(f"overruled: ok — treatment: resolving, {snapshots.HEARTBEAT_SECONDS} seconds", out)
        self.assertIn(f"overruled: ok — treatment: 6 rows over 3 clusters, {snapshots.HEARTBEAT_SECONDS} seconds", out)

    def test_overruled_reports_all_unresolved_without_a_count_gate(self) -> None:
        self._enable_stage()
        source, _, _ = self._add_captured_source()
        self.host.overruled_counts = {
            "list_rows": 4, "rows": 7, "clusters": 0,
            "overruled": {"absent": 7}, "overruling": {"ambiguous": 7},
        }
        code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 0, out + err)
        self.assertIn("overruled resolved 0 (cite 0, name 0), unresolved 7 (ambiguous 0, absent 7)", out)
        self.assertIn("overruling resolved 0 (cite 0, docket 0), unresolved 7", out)
        self.assertIn("overruled: ok — treatment: 7 rows over 0 clusters, 0 seconds", out)
        self.assertIn("retain: ok", out)

    def test_overruled_refuses_without_scotus_and_names_the_derived_cut(self) -> None:
        self._enable_stage()
        source, _, _ = self._add_captured_source()
        lock_path = self.checkout / "corpus/lockfiles" / f"{self.labels[0]}.yaml"
        loaded = load_lockfile(lock_path, known_sources={self.source.name: self.source, source.name: source})
        assert loaded.lockfile is not None
        lockfile = loaded.lockfile
        lock_path.write_text(render_lockfile(replace(
            lockfile, sources={**lockfile.sources, "example": replace(lockfile.sources["example"], courts=("ca6",))},
        )))
        code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 1, out + err)
        self.assertIn("the known-overruled list is Supreme Court only and scotus is not among the lockfile's courts", out)
        self.assertIn(report.command(f"corpus cut --base {self.labels[0]} --add-courts scotus"), out)
        self.assertIn(f"then run {report.command('corpus install')} with the new label", out)
        self.assertEqual(self.host.overruled_deferred, [])
        self.assertEqual(self.host.agreement_deferred, [])
        self.assertNotIn("retain: ok", out)

    def test_overruled_refuses_a_capture_without_any_case_law_courts(self) -> None:
        source, _, _ = self._add_captured_source()
        loaded = load_lockfile(
            self.checkout / "corpus/lockfiles" / f"{self.labels[0]}.yaml",
            known_sources={self.source.name: self.source, source.name: source},
        )
        assert loaded.lockfile is not None
        lockfile = replace(loaded.lockfile, sources={
            **loaded.lockfile.sources,
            self.source.name: replace(loaded.lockfile.sources[self.source.name], courts=None),
        })
        out_stream = io.StringIO()
        with contextlib.redirect_stdout(out_stream):
            code = install._overruled(self.host, RENDERED, self.work, lockfile,
                                     lambda _seconds: None, lambda: 0.0)
        out = out_stream.getvalue()
        self.assertEqual(code, 1, out)
        self.assertIn("overruled: refuse", out)
        self.assertIn("--add-courts scotus", out)
        self.assertEqual(self.host.overruled_deferred, [])

    def test_overruled_reads_the_matching_stage_before_defer(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        source, _, _ = self._add_captured_source()
        complete = staging.read_record(self.host, self.labels[0], "example", work_root=self.work)
        assert isinstance(complete, staging.StageRecord)
        for issue in (
            None, replace(complete, courts=["scotus"]),
            replace(complete, snapshot="example-2099-01-01"),
            report.Problem("fictitious stage read failed", "Run the fictitious stage fix."),
        ):
            with self.subTest(issue=issue):
                with patch.object(install.staging, "read_record", side_effect=[complete, complete, issue]):
                    code, out, err = self.run_install(sources=(self.source, source))
                self.assertEqual(code, 1, out + err)
                self.assertIn("overruled: refuse", out)
                self.assertEqual(self.host.overruled_deferred, [])
                self.assertEqual(self.host.agreement_deferred, [])
                if isinstance(issue, report.Problem):
                    self.assertIn(issue.fix, out)
                else:
                    self.assertIn(f"Remove {self.work / self.labels[0] / 'example'}", out)

    def test_overruled_defer_and_read_problems_preserve_their_fixes(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        source, _, _ = self._add_captured_source()
        issue = report.Problem("fictitious list job failed", "Run the fictitious job fix.")
        for operation in ("defer_overruled", "read_overruled"):
            with self.subTest(operation=operation), patch.object(install.overruled, operation, return_value=issue):
                code, out, err = self.run_install(sources=(self.source, source))
            self.assertEqual(code, 1, out + err)
            self.assertIn("overruled: refuse — treatment: fictitious list job failed", out)
            self.assertIn(issue.fix, out)
            self.assertEqual(self.host.overruled_count_reads, [])
            self.assertEqual(self.host.agreement_deferred, [])

    def test_failed_overruled_job_uses_its_fix_and_never_reads_counts(self) -> None:
        self._enable_stage()
        source, _, _ = self._add_captured_source()
        self.host.overruled_failure = "database"
        code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 1, out + err)
        self.assertIn("overruled: refuse — treatment: overruled database failure", out)
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.overruled_count_reads, [])
        self.assertEqual(self.host.agreement_deferred, [])

    def test_overruled_counts_problem_is_read_only_after_job_success(self) -> None:
        self._enable_stage()
        source, _, _ = self._add_captured_source()
        issue = report.Problem("fictitious counts read failed", "Run the fictitious counts fix.")

        def counts_problem(*_args: object, **_kwargs: object) -> report.Problem:
            self.assertEqual(self.host.overruled_events, ["defer", "read", "read"])
            return issue

        with patch.object(install.overruled, "read_overruled_counts", side_effect=counts_problem):
            code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 1, out + err)
        self.assertIn("overruled: refuse — treatment: fictitious counts read failed", out)
        self.assertIn(issue.fix, out)
        self.assertEqual(self.host.agreement_deferred, [])
        self.assertNotIn("retain: ok", out)

    def test_overruled_zero_rows_after_success_refuses_with_worker_logs(self) -> None:
        self._enable_stage()
        source, _, _ = self._add_captured_source()
        self.host.overruled_counts = {"list_rows": 0, "rows": 0, "clusters": 0, "overruled": {}, "overruling": {}}
        code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 1, out + err)
        self.assertIn("job succeeded but the record holds zero rows", out)
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.overruled_events, ["defer", "read", "read", "counts"])
        self.assertNotIn("agreement: ok", out)
        self.assertNotIn("retain: ok", out)

    def test_interrupt_during_overruled_rejoins_the_worker_job(self) -> None:
        self._enable_stage()
        source, _, _ = self._add_captured_source()
        self.host.interrupt_overruled_job = True
        code, out, err = self.run_install(sources=(self.source, source))
        self.assertEqual(code, 1, out + err)
        self.assertIn("overruled: refuse — interrupted; the overruled job continues in the worker", out)
        self.assertIn(f"Run {report.command('corpus install')} again to rejoin it", out)
        self.assertEqual(self.host.lock_releases, 1)
        self.assertEqual(self.host.overruled_count_reads, [])

    def test_a_projection_past_the_refusal_stops_the_build_before_any_index_job(self) -> None:
        self.host.projection_counts = {"ready": 2, "chunked": 1, "chunks": 10**9}
        code, out, err = self.run_install()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("guard: refuse", out)
        self.assertIn("projected chunks 2000000000", out)
        self.assertIn(report.command("corpus cut"), out)
        self.assertNotIn("reserve:", out)
        self.assertNotIn("cluster:", out)
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.reports, [])
        self.assertIsNone(self.host.lock_holder)

    def test_without_treatment_the_overruled_stage_prints_nothing(self) -> None:
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("overruled:", out)
        self._assert_build_rows(out)
        self.assertEqual(self.host.overruled_deferred, [])
        self.assertEqual(self.host.overruled_count_reads, [])

    def test_preconditions_refuse_a_down_store_with_the_install_fix(self) -> None:
        self.host.health[QDRANT_SERVICE_NAME] = "unhealthy"
        code, out, err = self.run_install()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("vector store is not running and healthy", err)
        self.assertIn(stack.logs_fix(RENDERED, QDRANT_SERVICE_NAME), err)
        self.assertIn(report.command("corpus install"), err)
        self.assertEqual(self.host.deferred_urls, [])
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.record_writes, 0)
        self.assertIsNone(self.host.lock_holder)

    def test_preconditions_refuse_a_missing_embed_pin_with_the_install_fix(self) -> None:
        path = self.checkout / "models.lock"
        data = yaml.safe_load(path.read_text())
        profile = data["profiles"][data["reference"]]
        del profile["models"][EMBED.role]
        del profile["embedding_space"]
        del profile["memory"][EMBED.service_name]
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        loaded = models.load_models_lock(path)
        self.assertTrue(loaded.ok, models.render_errors(loaded.errors))
        assert loaded.lock is not None
        code, out, err = self.run_install()
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"models.lock profile '{loaded.lock.reference}' has no embed pin", err)
        self.assertIn(report.command("corpus install"), err)
        self.assertEqual(self.host.deferred_urls, [])
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.record_writes, 0)
        self.assertIsNone(self.host.lock_holder)

    def test_preconditions_refuse_an_unknown_profile_with_the_install_fix(self) -> None:
        unknown = "1000x96v-256d"
        self.host.site_text = (ROOT / "config/site.example.yaml").read_text() + f"\nhardware_profile: {unknown}\n"
        code, out, err = self.run_install()
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"Hardware profile '{unknown}' is not in models.lock", err)
        self.assertIn(report.command("corpus install"), err)
        self.assertEqual(self.host.deferred_urls, [])
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.record_writes, 0)
        self.assertIsNone(self.host.lock_holder)

    def test_preconditions_refuse_unregistered_pipeline_before_fetch(self) -> None:
        path = self.checkout / "corpus/lockfiles" / f"{self.labels[0]}.yaml"
        loaded = load_lockfile(path, known_sources={self.source.name: self.source})
        assert loaded.lockfile is not None
        path.write_text(render_lockfile(replace(loaded.lockfile, pipeline="9.9.9")))
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("pipeline 9.9.9 is not registered", err)
        self.assertIn(worker_identity.PIPELINE_VERSION, err)
        self.assertIn(report.command("corpus cut"), err)
        self.assertFalse(self.host.deferred_urls)
        self.assertFalse(self.host.caselaw_deferred)

    def test_stage_rows_follow_record_and_summarize_the_deferred_job(self) -> None:
        self._enable_stage()
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertLess(out.index("record: ok"), out.index("stage: ok"))
        self.assertLess(out.index("stage: ok"), out.index("ingest: ok"))
        self.assertLess(out.index("ingest: ok"), out.index("agreement: ok"))
        self.assertLess(out.index("agreement: ok"), out.index("retain: ok"))
        self._assert_build_rows(out)
        self.assertIn("ca6: dockets 1, opinion-clusters 1, citations 1, opinions 1", out)
        self.assertIn("scotus: dockets 2, opinion-clusters 2, citations 2, opinions 2", out)
        self.assertIn("example: 2 courts staged, 15 records read, 8 seconds; counts recorded", out)
        self.assertEqual(len(self.host.stage_deferred), 1)
        self.assertEqual(self.host.stage_deferred[0]["courts"], ["ca6", "scotus"])
        self.assertEqual(tuple(self.host.stage_deferred[0]["inputs"]), worker_identity.STAGE_TABLES)
        job, args = next(iter(self.host.stage_jobs.items()))
        staged = self.host.stage_record(job, args)
        self.assertEqual(self.host.stage_counts_rows, {
            (staged["label"], staged["source"], court): {
                "label": staged["label"], "source": staged["source"], "court": court,
                **{table.replace("-", "_"): staged["counts"][court][table]
                   for table in worker_identity.STAGE_TABLES[1:]},
                "staged_at": staged["staged_at"],
            } for court in staged["courts"]
        })
        self.assertEqual([args["court"] for args in self.host.caselaw_deferred], ["ca6", "scotus"])
        self.assertEqual(self.host.caselaw_events[:2], [("defer", "ca6"), ("defer", "scotus")])
        self.assertTrue(all(set(args) == {"label", "snapshot", "court", "courts", "pipeline"}
                            for args in self.host.caselaw_deferred))
        self.assertTrue(all(args["pipeline"] == worker_identity.PIPELINE_VERSION
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
        self.assertEqual([args["court"] for args in self.host.agreement_deferred], ["ca6", "scotus"])
        self.assertTrue(all(
            set(args) == {"label", "snapshot", "court", "input"}
            for args in self.host.agreement_deferred
        ))
        map_name = data_file(worker_identity.AGREEMENT_TABLE, "2099-01-02")
        map_bytes = (self.snapshots / "example-2099-01-02" / map_name).read_bytes()
        self.assertTrue(all(
            args["input"] == {"path": map_name, "sha256": hashlib.sha256(map_bytes).hexdigest()}
            for args in self.host.agreement_deferred
        ))
        self.assertEqual(
            self.host.agreement_events[:2], [("defer", "ca6"), ("defer", "scotus")],
        )

    def test_complete_matching_stage_prints_courts_and_defers_nothing(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertIn("ca6: dockets 1, opinion-clusters 1, citations 1, opinions 1", out)
        self.assertIn("scotus: dockets 2, opinion-clusters 2, citations 2, opinions 2", out)
        self.assertIn("example: complete; nothing deferred; counts recorded", out)
        self.assertEqual(len(self.host.stage_counts_rows), len(self._lockfile().courts))
        self.assertEqual(self.host.stage_deferred, [])
        again, rerun, _ = self.run_install()
        self.assertEqual(again, 0, rerun)
        self.assertIn(f"stage: ok — {self.labels[0]} is installed; nothing to do", rerun)
        self.assertFalse((self.work / self.labels[0]).exists())
        self.assertEqual(self.host.stage_deferred, [])

    def test_stage_repeat_keeps_recorded_counts_and_original_timestamp(self) -> None:
        self.host.interrupt_caselaw_job = True
        code, out, err = self.run_install()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("counts recorded", out)
        recorded = copy.deepcopy(self.host.stage_counts_rows)
        directory = staging.work_directory(self.labels[0], self.source.name, work_root=self.work)
        path = directory / worker_identity.STAGE_RECORD_NAME
        staged = json.loads(path.read_text())
        staged["staged_at"] = (NOW + timedelta(days=1)).isoformat()
        path.write_text(json.dumps(staged))
        self.host.interrupt_caselaw_job = False
        code, out, err = self.run_install()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("example: complete; nothing deferred; counts already recorded", out)
        self.assertEqual(self.host.stage_counts_rows, recorded)
        self.assertEqual(len(self.host.stage_deferred), 1)

    def test_stage_counts_conflict_refuses_before_ingest_without_partial_inserts(self) -> None:
        directory = self._seed_complete_stage()
        staged = json.loads((directory / worker_identity.STAGE_RECORD_NAME).read_text())
        court = staged["courts"][-1]
        self.host.stage_counts_rows[(self.labels[0], self.source.name, court)] = {
            "label": self.labels[0], "source": self.source.name, "court": court,
            **{table.replace("-", "_"): staged["counts"][court][table]
               for table in worker_identity.STAGE_TABLES[1:]},
            "opinions": staged["counts"][court]["opinions"] + 1,
            "staged_at": staged["staged_at"],
        }
        recorded = copy.deepcopy(self.host.stage_counts_rows)
        code, out, err = self.run_install()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn(f"stage: refuse — example: stage counts for {court} differ from the recorded ones", out)
        self.assertIn(report.command("corpus cut"), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertNotIn("<label>", out)
        self.assertNotIn("ingest:", out)
        self.assertEqual(self.host.stage_counts_rows, recorded)
        self.assertEqual(self.host.stage_deferred, [])
        self.assertEqual(self.host.caselaw_deferred, [])

    def test_stage_counts_write_failure_preserves_database_fix(self) -> None:
        self.host.fail_phase = "stage-counts-write"
        code, out, err = self.run_install()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("stage: refuse", out)
        self.assertIn("corpus record write failed (exit 3)", out)
        self.assertIn(stack.logs_fix(RENDERED, "postgres"), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.stage_counts_rows, {})
        self.assertNotIn("ingest:", out)

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
        self.assertEqual(sleeps, [1.0, 1.0])
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

    def test_agreement_prints_exact_court_rows_and_summary_without_count_gate(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.agreement_figures["scotus"] = {
            "documents": 0, "gideon_pairs": 0, "map_rows": 7,
            "map_outside": 7, "map_pairs": 0, "agreed": 0,
            "gideon_only": 0, "map_only_seen": 0, "map_only_missed": 0,
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertEqual(
            [line for line in out.splitlines() if line.startswith("agreement: ok — ")],
            [
                "agreement: ok — ca6 agreement: 1 documents; 4 GIDEON pairs, "
                "3 map pairs, 1 map rows beyond the label; agreed 2 "
                "(50 % of GIDEON's, 67 % of the map's); "
                "map-only 1 seen unresolved, 0 missed",
                "agreement: ok — scotus agreement: 0 documents; 0 GIDEON pairs, "
                "0 map pairs, 7 map rows beyond the label; agreed 0 "
                "(0 % of GIDEON's, 0 % of the map's); "
                "map-only 0 seen unresolved, 0 missed",
                "agreement: ok — caselaw: 2 courts measured, 0 seconds",
            ],
        )
        self.assertEqual(self.host.agreement_events[:2],
                         [("defer", "ca6"), ("defer", "scotus")])

    def test_agreement_waits_once_per_round_and_reports_a_minute_heartbeat(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        now = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += snapshots.HEARTBEAT_SECONDS

        code, out, err = self.run_install(sleep=sleep, monotonic=lambda: now[0])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sleeps, [1.0, 1.0])
        self.assertIn(
            f"agreement: ok — caselaw: measuring, 2 courts, {snapshots.HEARTBEAT_SECONDS} seconds",
            out,
        )
        self.assertIn(
            f"agreement: ok — caselaw: 2 courts measured, {snapshots.HEARTBEAT_SECONDS} seconds",
            out,
        )
        self.assertEqual(
            self.host.agreement_events[:6],
            [("defer", "ca6"), ("defer", "scotus"),
             ("read", "ca6"), ("read", "scotus"),
             ("read", "ca6"), ("read", "scotus")],
        )

    def test_failed_agreement_job_refuses_with_its_fix(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.agreement_failure = "database"
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("agreement: refuse — ca6: agreement database failure", out)
        for command in ("host provision", "apply", "corpus install"):
            self.assertIn(report.command(command), out)
        self.assertNotIn("retain: ok", out)

    def test_agreement_problem_refuses_with_its_fix(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        issue = report.Problem("comparison read failed", "Run the fictitious fix.")
        with patch.object(install.agreement, "read_agreement", return_value=issue):
            code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("agreement: refuse — ca6: comparison read failed", out)
        self.assertIn(issue.fix, out)
        self.assertNotIn("retain: ok", out)

    def test_missing_pinned_map_refuses_before_agreement_defer(self) -> None:
        self._enable_stage(missing=worker_identity.AGREEMENT_TABLE)
        self._seed_complete_stage()
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("agreement: refuse", out)
        self.assertIn(data_file(worker_identity.AGREEMENT_TABLE, "2099-01-02"), out)
        self.assertIn(report.command("corpus cut"), out)
        self.assertEqual(self.host.agreement_deferred, [])

    def test_agreement_reads_stage_record_before_deferring(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        complete = staging.read_record(
            self.host, self.labels[0], "example", work_root=self.work,
            command_path=install.COMMAND_PATH,
        )
        assert isinstance(complete, staging.StageRecord)
        for issue in (None, replace(complete, courts=["ca6"])):
            with self.subTest(issue=issue):
                self.host.agreement_deferred.clear()
                with patch.object(install.staging, "read_record", side_effect=[complete, complete, issue]):
                    code, out, err = self.run_install()
                self.assertEqual(code, 1, out + err)
                self.assertIn("agreement: refuse", out)
                self.assertEqual(self.host.agreement_deferred, [])
                self.assertIn(f"Remove {self.work / self.labels[0] / 'example'}", out)

    def test_interrupt_during_agreement_reports_worker_continuation(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.interrupt_agreement_job = True
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn("agreement: refuse", out)
        self.assertIn("interrupted; agreement jobs continue in the worker", out)
        self.assertIn(report.command("corpus install"), out)
        self.assertEqual(self.host.lock_releases, 1)

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
        self.host.caselaw_chunk_counts = {
            "ca6": {
                "ready": 0, "chunked": 0, "chunks": 0, "by_section_type": {},
                "token_minimum": None, "token_median": None,
                "token_p95": None, "token_maximum": None,
                "over_maximum": 0, "under_minimum": 0,
            },
            "scotus": {
                "ready": 2, "chunked": 2, "chunks": 3,
                "by_section_type": {"majority": 2, "footnote": 1},
                "token_minimum": 42, "token_median": 510.0,
                "token_p95": 900.5, "token_maximum": 944,
                "over_maximum": 0, "under_minimum": 1,
            },
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        section_shares = {"majority": "60 % (3)", "footnote": "40 % (1)"}
        chunk_types = {"majority": 2, "footnote": 1}
        target, maximum, _minimum = worker_identity.PIPELINE_CHUNK_SIZES[
            worker_identity.PIPELINE_VERSION
        ]
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
            "ingest: ok — ca6 chunks: 0 of 0 ready documents under "
            + worker_identity.PIPELINE_VERSION
            + f"; 0 chunks, 0.0 per document; tokens minimum -, median -, p95 -, "
            f"maximum - (target {target}, maximum {maximum}); over maximum 0, "
            "under minimum 0; "
            + ", ".join(f"{kind} 0" for kind in worker_identity.SECTION_TYPES),
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
            "ingest: ok — scotus chunks: 2 of 2 ready documents under "
            + worker_identity.PIPELINE_VERSION
            + f"; 3 chunks, 1.5 per document; tokens minimum 42, median 510, "
            f"p95 900.5, maximum 944 (target {target}, maximum {maximum}); "
            "over maximum 0, under minimum 1; "
            + ", ".join(
                f"{kind} {chunk_types.get(kind, 0)}"
                for kind in worker_identity.SECTION_TYPES
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

    def test_ingest_refuses_ready_documents_without_chunks(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        self.host.caselaw_chunk_counts["ca6"] = {
            "ready": 1, "chunked": 0, "chunks": 0, "by_section_type": {},
            "token_minimum": None, "token_median": None, "token_p95": None,
            "token_maximum": None, "over_maximum": 0, "under_minimum": 0,
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn(
            f"ingest: refuse — ca6: 0 of 1 ready documents have chunks under "
            f"{worker_identity.PIPELINE_VERSION}", out,
        )
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertNotIn("ca6 chunks:", out)
        self.assertNotIn("retain: ok", out)

    def test_ingest_refuses_chunks_over_the_pipeline_maximum(self) -> None:
        self._enable_stage()
        self._seed_complete_stage()
        _target, maximum, _minimum = worker_identity.PIPELINE_CHUNK_SIZES[
            worker_identity.PIPELINE_VERSION
        ]
        self.host.caselaw_chunk_counts["ca6"] = {
            "ready": 1, "chunked": 1, "chunks": 1,
            "by_section_type": {"majority": 1},
            "token_minimum": maximum + 1, "token_median": maximum + 1,
            "token_p95": maximum + 1, "token_maximum": maximum + 1,
            "over_maximum": 1, "under_minimum": 0,
        }
        code, out, err = self.run_install()
        self.assertEqual(code, 1, out + err)
        self.assertIn(
            f"ingest: refuse — ca6: 1 chunks exceed pipeline "
            f"{worker_identity.PIPELINE_VERSION}'s maximum", out,
        )
        self.assertIn(stack.logs_fix(RENDERED, worker_identity.WORKER_SERVICE_NAME), out)
        self.assertIn(report.command("corpus install"), out)
        self.assertNotIn("ca6 chunks:", out)
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
        self.assertEqual(self.host.record_rows[self.labels[0]]["state"], "installed")
        self.assertEqual(self.host.record_rows[self.labels[0]]["installed_at"], NOW.isoformat())

    def test_held_installing_and_installed_states_stay(self) -> None:
        for state in ("installing", "installed"):
            with self.subTest(state=state):
                self._hold_record(state)
                code, out, err = self.run_install()
                self.assertEqual(code, 0, out + err)
                self.assertIn(f"record: ok — {self.labels[0]}: {state}", out)
                self.assertEqual(self.host.record_rows[self.labels[0]]["state"], "installed")
                self.assertEqual(self.host.record_rows[self.labels[0]]["installed_at"], NOW.isoformat())

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
                               known_sources={self.source.name: self.source})
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
        self.assertEqual(self.host.record_rows[self.labels[1]]["state"], "installed")
        self.assertEqual(self.host.record_rows[self.labels[0]]["state"], "superseded")

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
        self.assertIn(
            f"verify: ok — {self._lockfile().sources[self.source.name].files} files "
            "match fetch records and pinned sidecars", out,
        )
        self.assertEqual(self.host.deferred_urls, [base_file_url])
        self.assertEqual(tuple(self.host.job_polls[job] for job in self.host.job_destinations), (2,))
        self.assertEqual(self.file.read_bytes(), self.pinned_bytes)

    def test_absent_file_fetches_from_mirror_and_verifies(self) -> None:
        self._set_mirror()
        self._remove_snapshot()
        mirror_file_url = f"{self.mirror_url}{FILE_PATH}"
        self.host.served_bytes[mirror_file_url] = self.pinned_bytes
        code, out, err = self.run_install()
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 fetch deferred", out)
        self.assertIn(
            f"verify: ok — {self._lockfile().sources[self.source.name].files} files "
            "match fetch records and pinned sidecars", out,
        )
        self.assertEqual(self.host.deferred_urls, [mirror_file_url])
        self.assertEqual(tuple(self.host.job_polls[job] for job in self.host.job_destinations), (2,))
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
        self.assertIn(
            f"verify: ok — {self._lockfile().sources[self.source.name].files} files "
            "match fetch records and pinned sidecars", out,
        )
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
        self._hold_record("cut", second)
        code, out, err = self.run_install(second, clock=lambda: NOW + timedelta(days=1))
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"2 kept ({first}, {second}), 0 removed, 1 unpinned", out)

        self.assertEqual(self.host.record_rows[first]["state"], "superseded")
        self.assertEqual(self.host.record_rows[first]["installed_at"], NOW.isoformat())
        self._hold_record("cut", third)
        code, out, err = self.run_install(third, clock=lambda: NOW + timedelta(days=2))
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"retain: ok — removed {first_dir}", out)
        self.assertIn(f"2 kept ({second}, {third}), 1 removed, 0 unpinned", out)
        self.assertFalse(first_dir.exists())
        self.assertEqual(self.host.record_rows[second]["state"], "superseded")
        self.assertEqual(self.host.record_rows[second]["installed_at"], (NOW + timedelta(days=1)).isoformat())
        self.assertEqual(self.host.record_rows[third]["installed_at"], (NOW + timedelta(days=2)).isoformat())
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
            known_sources={self.source.name: self.source},
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
        self.seed_report_work(third)
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
