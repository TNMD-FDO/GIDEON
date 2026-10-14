"""The index build command over fictitious lockfiles and a recorded queue."""

import argparse
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import unittest
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from typing import Any

import test_corpus_install
import yaml  # type: ignore[import-untyped]
from test_corpus_install import NOW, RENDERED, ROOT, FakeHost
from test_engine_verify import models_text
from test_models_lock import VALID, VALID_WITH_SPACE

from gideon.host import backuplock, models, report, stack
from gideon.host.corpus.lockfile import load_lockfile, render_lockfile
from gideon.host.corpus.sources import SOURCES
from gideon.host.index import build, dense, generations
from gideon.host.index.record import GenerationRow
from gideon.host.render.engine import EMBED
from gideon.host.render.opensearch import OPENSEARCH_SERVICE_NAME
from gideon.host.render.qdrant import QDRANT_SERVICE_NAME
from gideon.host.render.worker import (
    DENSE_LOCK_PREFIX,
    DENSE_TASK,
    LEXICAL_TASK,
    WORKER_SERVICE_NAME,
)
from gideon.host.sysio import Command, PathLike

SITE_PATH = "/etc/gideon/site.yaml"


class BuildHost(FakeHost):
    """Extend the corpus fixture host with generation records and both build jobs."""

    def __init__(self) -> None:
        super().__init__()
        self.site_text: str | None = (ROOT / "config/site.example.yaml").read_text() + "\nhardware_profile: 1x1v-1d\n"
        self.declared: tuple[str, ...] = (WORKER_SERVICE_NAME, OPENSEARCH_SERVICE_NAME, QDRANT_SERVICE_NAME, EMBED.service_name)
        self.health = dict.fromkeys(self.declared, "healthy")
        self.ps_reads = 0
        self.store_state_code = 0
        self.store_state_error: OSError | subprocess.SubprocessError | None = None
        self.store_state_text: str | None = None
        self.state_read = 2
        self.generations: dict[str, GenerationRow] = {}
        self.jobs: dict[int, dict[str, Any]] = {}
        self.deferred: list[dict[str, Any]] = []
        self.live_id: int | None = None
        self.dense_live_id: int | None = None
        self.task_by_job: dict[int, str] = {}
        self.active_task = LEXICAL_TASK
        self.pending_polls = 0
        self.dense_pending_polls = 0
        self.failure_reason: str | None = None
        self.dense_failure_reason: str | None = None
        self.finish_stamp: str | None = NOW.isoformat()
        self.dense_finish_stamp: str | None = NOW.isoformat()
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

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        if str(path) == SITE_PATH:
            if self.site_text is None:
                raise FileNotFoundError(str(path))
            return self.site_text
        if str(path) == f"{RENDERED}/compose.yaml":
            return yaml.safe_dump({"services": {name: {} for name in self.declared}})
        return super().read_text(path, encoding=encoding)

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        if command == stack.compose_argv(RENDERED, "ps", "--all", "--format", "json"):
            self.calls.append(command)
            self.ps_reads += 1
            if self.interrupt_phase == "preconditions":
                raise KeyboardInterrupt
            if self.ps_reads == self.state_read and self.store_state_error is not None:
                raise self.store_state_error
            text = json.dumps([{"Service": name, "State": "running", "Health": self.health[name]} for name in self.health])
            if self.ps_reads == self.state_read and self.store_state_text is not None:
                text = self.store_state_text
            return subprocess.CompletedProcess(command, self.store_state_code if self.ps_reads == self.state_read else 0, text, "private diagnostic")
        if input is not None and (
            "public.index_generations" in input or "procrastinate_defer_jobs_v1" in input
            or "SELECT id FROM procrastinate_jobs" in input or "SELECT id, status, attempts" in input
        ):
            self.calls.append(command)
            self.statements.append(input)
            if "DO $generation$" in input:
                phase = "generation"
            elif "public.index_generations" in input:
                phase = "dense-counts" if self.active_task == DENSE_TASK else "counts"
            elif "SELECT id FROM procrastinate_jobs" in input:
                self.active_task = DENSE_TASK if self._bound(input, "v_lock").startswith(DENSE_LOCK_PREFIX) else LEXICAL_TASK
                phase = "dense-live-job" if self.active_task == DENSE_TASK else "live-job"
            elif "procrastinate_defer_jobs_v1" in input:
                self.active_task = self._bound(input, "v_task")
                phase = "dense-defer" if self.active_task == DENSE_TASK else "defer"
            else:
                self.active_task = self.task_by_job[int(self._bound(input, "v_job_id"))]
                phase = "dense" if self.active_task == DENSE_TASK else "build"
            if self.interrupt_phase == phase:
                raise KeyboardInterrupt
            if self.fail_phase == phase:
                return subprocess.CompletedProcess(command, 3, "", "private diagnostic")
            if phase == "generation":
                payload = json.loads(self._bound(input, "v_payload"))
                row = next((row for row in self.generations.values() if row.collection == payload["collection"] and row.inputs_hash == payload["inputs_hash"]), None)
                outcome = "found" if row is not None else "created"
                if row is None:
                    row = GenerationRow(
                        payload["name"], payload["collection"], payload["label"], (payload["label"],),
                        payload["pipeline"], payload["embedding_space"], payload["inputs_hash"],
                        "building", None, self.counts, 1024, None, None, None, None, None, NOW.isoformat(),
                    )
                elif payload["label"] not in row.lockfiles:
                    row = replace(row, lockfiles=(*row.lockfiles, payload["label"]))
                self.generations[row.gen_id] = row
                notice = {"outcome": outcome, "name": row.gen_id, "state": row.state}
                return subprocess.CompletedProcess(command, 0, "", "NOTICE: INDEX_GENERATION:" + json.dumps(notice) + "\n")
            if phase in {"counts", "dense-counts"}:
                self.generation_reads += 1
                row = self.generations.get(self._bound(input, "v_name"))
                return subprocess.CompletedProcess(command, 0, json.dumps(asdict(row)) if row is not None else "", "")
            if phase in {"live-job", "dense-live-job"}:
                live_id = self.dense_live_id if self.active_task == DENSE_TASK else self.live_id
                return subprocess.CompletedProcess(command, 0, str(live_id) if live_id is not None else "", "")
            if phase in {"defer", "dense-defer"}:
                args = json.loads(self._bound(input, "v_args"))
                self.next_job += 1
                self.jobs[self.next_job] = args
                self.task_by_job[self.next_job] = self.active_task
                self.deferred.append(args)
                if self.active_task == DENSE_TASK:
                    self.dense_live_id = self.next_job
                else:
                    self.live_id = self.next_job
                return subprocess.CompletedProcess(command, 0, str(self.next_job), "")
            job = int(self._bound(input, "v_job_id"))
            if self.active_task == DENSE_TASK:
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
                    state=row.state if pending else "failed" if self.dense_failure_reason else
                    "built" if self.dense_finish_stamp and row.lexical_built_at else "building",
                    failure_reason=self.dense_failure_reason if not pending else None,
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
                    row, lexical_built_at=self.finish_stamp,
                    state="failed" if self.failure_reason else "built" if row.dense_built_at else "building",
                    failure_reason=self.failure_reason,
                )
                self.live_id = None
            return subprocess.CompletedProcess(command, 0, f"{job}|{status}|1", "")
        return super().run(argv, check=check, input=input, cwd=cwd, env=env, timeout=timeout, passthrough=passthrough)

class Build(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_corpus_install.Install()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture._enable_stage()
        self.host = BuildHost()
        self.fixture.host = self.host
        for label in self.fixture.labels:
            self.fixture._hold_record("cut", label)
        (self.fixture.checkout / "models.lock").write_text(models_text())
        self.time = 0.0

    def run_build(
        self, label: str | None = None, *, sample: bool = False,
        collection: str = "caselaw", sleep: Callable[[float], None] | None = None,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()

        def tick(seconds: float) -> None:
            self.time += seconds

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build.run_index_build(
                argparse.Namespace(collection=collection, lockfile=label, sample=sample),
                host=self.host, rendered_dir=RENDERED, checkout=self.fixture.checkout,
                sources=(self.fixture.source,), clock=lambda: NOW,
                monotonic=lambda: self.time, sleep=sleep or tick,
            )
        return code, out.getvalue(), err.getvalue()

    def inputs(self, label: str) -> generations.GenerationInputs:
        loaded = load_lockfile(self.fixture.checkout / f"corpus/lockfiles/{label}.yaml", known_sources={"example": replace(SOURCES[0], name="example")})
        assert loaded.lockfile is not None
        lockfile = loaded.lockfile
        model_lock = models.load_models_lock(self.fixture.checkout / "models.lock")
        assert model_lock.lock is not None
        profile = models.select_profile(model_lock.lock, model_lock.lock.reference)
        assert isinstance(profile, models.HardwareProfile) and profile.embedding_space is not None
        pin = lockfile.sources[self.fixture.source.name]
        return generations.GenerationInputs(
            generations.generation_name("caselaw", label, profile.embedding_space.id), "caselaw", label,
            f"{self.fixture.source.name}-{pin.snapshot_date}", tuple(pin.courts or ()), {},
            lockfile.pipeline, profile.embedding_space.id,
            generations.inputs_hash(lockfile, "caselaw", profile.embedding_space.id, sources=(self.fixture.source,)),
        )

    def test_two_lockfiles_print_stages_and_record_distinct_generations(self) -> None:
        for label in self.fixture.labels:
            inputs = self.inputs(label)
            code, out, err = self.run_build(label)
            self.assertEqual((code, err), (0, ""), out)
            for stage in ("preconditions", "generation", "build", "counts", "dense", "built"):
                self.assertIn(f"{stage}: ok", out)
            self.assertIn(inputs.name, out)
            self.assertIn("building, created", out)
            self.assertIn("documents ready 3, chunked 2, unchunked 1", out)
            self.assertIn("chunks indexed 7, present 4, total 11", out)
            self.assertIn("bytes 1024", out)
            self.assertIn("points 11, indexed 11, written 8 (present 3, copied 2, embedded 6)", out)
            self.assertIn("state building", out)
            job = self.host.deferred[-1]
            self.assertEqual(job["snapshot"], inputs.snapshot)
            self.assertEqual(job["courts"].keys(), set(inputs.courts))
            self.assertEqual(job["inputs_hash"], inputs.inputs_hash)
            self.assertEqual(job["embedding_space"], inputs.embedding_space)
            self.assertIsNone(self.host.lock_holder)
        self.assertEqual(len(self.host.generations), 2)
        self.assertEqual(self.host.lock_releases, 2)

    def test_newest_default_and_sample_refusal_before_any_host_work(self) -> None:
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.host.deferred[0]["label"], self.fixture.labels[-1])
        self.host.calls.clear()
        self.host.euid = 1
        releases = self.host.lock_releases
        self.assertEqual(self.run_build(sample=True), (1, "", f"gideon index build --sample: not implemented; {build.SAMPLE_LANDING}\n"))
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.lock_releases, releases)

    def test_same_state_under_another_label_finds_the_original_generation(self) -> None:
        first, second = self.fixture.labels
        self.assertEqual(self.run_build(first)[0], 0)
        directory = self.fixture.checkout / "corpus/lockfiles"
        loaded = load_lockfile(directory / f"{first}.yaml", known_sources={"example": replace(SOURCES[0], name="example")})
        assert loaded.lockfile is not None
        shutil.copytree(directory / first, directory / second, dirs_exist_ok=True)
        (directory / f"{second}.yaml").write_text(render_lockfile(replace(loaded.lockfile, label=second)))
        code, out, err = self.run_build(second)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("built, found", out)
        self.assertEqual(len(self.host.generations), 1)
        self.assertEqual(self.host.deferred[-1]["label"], second)
        self.assertEqual(self.host.deferred[-1]["name"], self.host.deferred[0]["name"])
        self.assertEqual(next(iter(self.host.generations.values())).lockfiles, (first, second))

    def test_live_generation_ends_ok_and_retired_generation_refuses(self) -> None:
        label = self.fixture.labels[0]
        self.assertEqual(self.run_build(label)[0], 0)
        row = next(iter(self.host.generations.values()))
        for state in ("live", "retired"):
            self.host.generations[row.gen_id] = replace(row, state=state)
            self.host.deferred.clear()
            code, out, err = self.run_build(label)
            self.assertEqual(err, "")
            self.assertEqual(code, 0 if state == "live" else 1)
            self.assertIn("generation is live; nothing built" if state == "live" else "generation is retired", out)
            self.assertNotIn("counts:", out)
            self.assertEqual(self.host.deferred, [])
            self.assertIsNone(self.host.lock_holder)

    def test_rejoins_a_live_job_without_deferring_again(self) -> None:
        label = self.fixture.labels[0]
        self.assertEqual(self.run_build(label)[0], 0)
        self.host.live_id = next(iter(self.host.jobs))
        count = len(self.host.deferred)
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"rejoined job {self.host.live_id or next(iter(self.host.jobs))}", out)
        self.assertEqual(len(self.host.deferred), count + 1)

    def test_minute_heartbeat_reads_recorded_counts_without_real_waiting(self) -> None:
        self.host.pending_polls = 121
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        heartbeat = "building, documents 3, chunks indexed 7, seconds 5"
        self.assertEqual(out.count(heartbeat), 2)
        self.assertEqual(self.host.generation_reads, 5)
        self.assertEqual(self.time, 121)

    def test_failed_job_prints_its_reason_fix_and_can_be_resumed(self) -> None:
        self.host.failure_reason = "store"
        label = self.fixture.labels[0]
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("build: refuse", out)
        self.assertIn(stack.logs_fix(RENDERED, OPENSEARCH_SERVICE_NAME), out)
        self.assertNotIn("counts:", out)
        self.assertIn(report.command(f"index build caselaw --lockfile {label}"), out)
        self.host.failure_reason = None
        self.assertEqual(self.run_build(label)[0], 0)

    def test_succeeded_job_without_lexical_timestamp_refuses_counts(self) -> None:
        self.host.finish_stamp = None
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("counts: refuse", out)
        self.assertIn("no recorded lexical_built_at", out)
        self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), out)
        self.assertNotIn("dense:", out)

    def test_empty_index_is_reported(self) -> None:
        self.host.counts = dict.fromkeys(self.host.counts, 0)
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("empty index", out)

    def test_each_runtime_failure_stops_and_releases_the_lock(self) -> None:
        for phase in ("generation", "live-job", "defer", "build", "counts",
                      "dense-live-job", "dense-defer", "dense", "dense-counts"):
            with self.subTest(phase=phase):
                self.host.fail_phase = phase
                code, out, err = self.run_build()
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn("refuse", out)
                self.assertNotIn("private diagnostic", out)
                self.assertIsNone(self.host.lock_holder)
        self.host.fail_phase = None

    def test_interrupts_report_continuation_or_record_state_and_release(self) -> None:
        for phase, text in (("preconditions", "nothing was recorded"), ("generation", "nothing was recorded"),
                            ("build", "the build continues in the worker"), ("counts", "the row stands")):
            with self.subTest(phase=phase):
                self.host.interrupt_phase = phase
                code, out, err = self.run_build()
                self.assertEqual(code, 1)
                self.assertIn(text, out + err)
                self.assertIn("Fix:", out + err)
                self.assertIsNone(self.host.lock_holder)
        self.host.interrupt_phase = None

    def test_root_and_held_or_nested_locks_refuse(self) -> None:
        self.host.euid = 1
        code, out, err = self.run_build()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("root privileges", err)
        self.assertEqual(self.host.calls, [])
        self.host.euid = 0
        for pid in (os.getpid() + 1000, os.getpid()):
            holder = backuplock.Record("gideon corpus install", pid, NOW).to_json()
            self.host.lock_holder = holder
            code, out, err = self.run_build()
            self.assertEqual((code, out), (1, ""))
            self.assertIn("corpus", err)
            self.assertEqual(self.host.lock_holder, holder)
            self.assertEqual(self.host.lock_releases, 0)

    def test_unrecorded_and_superseded_lockfiles_refuse_but_cut_builds(self) -> None:
        label = self.fixture.labels[0]
        row = self.host.record_rows.pop(label)
        code, out, err = self.run_build(label)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not recorded", err)
        self.assertIn(report.command(f"corpus install {label}"), err)
        self.host.record_rows[label] = {**row, "state": "superseded"}
        self.assertIn("superseded", self.run_build(label)[2])
        self.host.record_rows[label] = row
        self.assertEqual(self.run_build(label)[0], 0)

    def test_missing_or_malformed_selection_and_artifacts_refuse(self) -> None:
        for label, collection, detail in (("bad-label", "caselaw", "label is invalid"),
                                         ("corpus-2099-02-30", "caselaw", "invalid date"),
                                         ("corpus-2099-02-03", "caselaw", "no matching committed"),
                                         (None, "unknown", "collection is not registered")):
            with self.subTest(label=label, collection=collection):
                code, out, err = self.run_build(label, collection=collection)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(detail, err)
                self.assertEqual(self.host.deferred, [])
                self.assertIsNone(self.host.lock_holder)
        path = self.fixture.checkout / "courts.yaml"
        path.unlink()
        self.assertIn("court map", self.run_build()[2])

    def test_worker_and_store_must_be_declared_and_healthy(self) -> None:
        for service in (WORKER_SERVICE_NAME, OPENSEARCH_SERVICE_NAME, QDRANT_SERVICE_NAME, EMBED.service_name):
            with self.subTest(service=service):
                self.host.declared = tuple(name for name in self.host.health if name != service)
                code, out, err = self.run_build()
                self.assertEqual((code, out), (1, ""))
                if service != WORKER_SERVICE_NAME:
                    self.assertIn(stack.logs_fix(RENDERED, service), err)
                self.host.declared = tuple(self.host.health)
                self.host.health[service] = "unhealthy"
                code, out, err = self.run_build()
                self.assertEqual((code, out), (1, ""))
                self.assertIn(stack.logs_fix(RENDERED, service), err)
                self.host.health[service] = "healthy"
                self.assertIsNone(self.host.lock_holder)
                self.assertEqual(self.host.deferred, [])

    def test_store_state_failures_and_missing_tool_refuse(self) -> None:
        for code, error, text in ((1, None, None), (127, None, None), (0, FileNotFoundError(), None),
                                  (0, subprocess.SubprocessError(), None), (0, None, "broken")):
            with self.subTest(code=code, error=error, text=text):
                self.host.ps_reads = 0
                self.host.store_state_code, self.host.store_state_error, self.host.store_state_text = code, error, text
                result, out, err = self.run_build()
                self.assertEqual((result, out), (1, ""))
                self.assertIn("Fix:", err)
                if code == 127 or error is not None:
                    self.assertIn("Restore Docker Compose", err)
                self.assertNotIn("private diagnostic", err)
                self.assertIsNone(self.host.lock_holder)

    def test_site_models_and_missing_embedding_space_refuse(self) -> None:
        path = self.fixture.checkout / "models.lock"
        for text in ("broken", VALID):
            with self.subTest(text=text[:20]):
                path.write_text(text)
                code, out, err = self.run_build()
                self.assertEqual((code, out), (1, ""))
                self.assertIn("models.lock", err)
                self.assertEqual(self.host.deferred, [])
        path.write_text(VALID_WITH_SPACE)
        self.host.site_text = None
        self.assertIn("site file is missing", self.run_build()[2])
        self.host.site_text = "broken"
        self.assertEqual(self.run_build()[0], 1)

    def test_selected_site_profile_supplies_the_space_served_name_and_dimensions(self) -> None:
        data = yaml.safe_load(models_text())
        reference = data["reference"]
        alternative = "1000x96v-256d"
        data["profiles"][alternative] = copy.deepcopy(data["profiles"][reference])
        data["profiles"][alternative]["embedding_space"]["id"] = "another-example-space"
        data["profiles"][alternative]["embedding_space"]["dimensions"] = 19
        data["profiles"][alternative]["models"]["embed"]["serve"]["served_name"] = "other-example-embed"
        (self.fixture.checkout / "models.lock").write_text(yaml.safe_dump(data, sort_keys=False))
        self.host.site_text = (ROOT / "config/site.example.yaml").read_text() + f"\nhardware_profile: {alternative}\n"
        loaded = models.load_models_lock(self.fixture.checkout / "models.lock")
        self.assertTrue(loaded.ok, models.render_errors(loaded.errors))
        assert loaded.lock is not None
        selected = models.select_profile(loaded.lock, alternative)
        assert isinstance(selected, models.HardwareProfile) and selected.embedding_space is not None
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.host.deferred[-1]["embedding_space"], selected.embedding_space.id)
        embed_pin = selected.model(EMBED.role)
        assert embed_pin is not None
        self.assertEqual(self.host.deferred[-1]["served_model"], embed_pin.serve.served_name)
        self.assertEqual(self.host.deferred[-1]["dimensions"], selected.embedding_space.dimensions)
        self.assertIn(f"embedding space {selected.embedding_space.id}; served model {embed_pin.serve.served_name}", out)
        self.assertNotIn("limit", self.host.deferred[-1])

    def test_three_services_refuse_bad_state_reads_without_retry(self) -> None:
        for read, service in enumerate((OPENSEARCH_SERVICE_NAME, QDRANT_SERVICE_NAME, EMBED.service_name), 2):
            for code, error, text in ((3, None, None), (127, None, None), (0, FileNotFoundError(), None),
                                      (0, subprocess.SubprocessError(), None), (0, None, "broken")):
                with self.subTest(service=service, code=code, error=error, text=text):
                    self.host.ps_reads = 0
                    self.host.state_read = read
                    self.host.store_state_code, self.host.store_state_error, self.host.store_state_text = code, error, text
                    result, out, err = self.run_build()
                    self.assertEqual((result, out), (1, ""))
                    self.assertIn(stack.logs_fix(RENDERED, service), err)
                    self.assertNotIn("private diagnostic", err)
                    self.assertEqual(self.host.ps_reads, read)
                    self.assertEqual(self.host.deferred, [])

    def test_missing_embed_pin_uses_engine_words(self) -> None:
        path = self.fixture.checkout / "models.lock"
        path.write_text(VALID_WITH_SPACE)
        self.host.site_text = (ROOT / "config/site.example.yaml").read_text()
        code, out, err = self.run_build()
        self.assertEqual((code, out), (1, ""))
        loaded = models.load_models_lock(path)
        assert loaded.lock is not None
        self.assertIn(f"models.lock profile '{loaded.lock.reference}' has no embed pin", err)
        self.assertIn(report.command("index build caselaw"), err)
        self.assertEqual(self.host.deferred, [])

    def test_dense_rejoins_its_own_live_lock(self) -> None:
        label = self.fixture.labels[0]
        self.assertEqual(self.run_build(label)[0], 0)
        job = next(job for job, task in self.host.task_by_job.items() if task == DENSE_TASK)
        self.host.dense_live_id = job
        count = len(self.host.deferred)
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"dense: ok — {self.host.jobs[job]['name']}: rejoined job {job}", out)
        self.assertIn(f"job {job} succeeded, attempts 2", out)
        self.assertEqual(len(self.host.deferred), count + 1)
        locks = [self.host._bound(sql, "v_lock") for sql in self.host.statements if "SELECT id FROM procrastinate_jobs" in sql]
        self.assertEqual(locks[-1], f"{DENSE_LOCK_PREFIX}{self.host.jobs[job]['name']}")

    def test_dense_heartbeat_first_hour_once_and_indexing(self) -> None:
        self.host.dense_pending_polls = 121
        self.host.dense_counts.update(dense_total=250, dense_scope=100, dense_seconds=4000)

        def progress(poll: int) -> None:
            if poll == 20:
                self.host.dense_counts.update(dense_hour_seconds=3600, dense_hour_tokens=720, dense_hour_written=8)
            if poll == 62:
                self.host.dense_counts.update(dense_scope=11, dense_indexed=4)

        self.host.dense_progress = progress
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("embedding, chunks written 8 of 100 (present 3, copied 2, embedded 6), tokens 120", out)
        self.assertIn("tok/s, projected finish", out)
        self.assertIn("indexing 4 of 11", out)
        self.assertEqual(out.count("first-hour: ok"), 1)
        self.assertIn("first hour: tokens 720 in 3600 s, 0.2 tok/s, written 8", out)
        self.assertIn("the generation of 250 chunks projected at", out)
        self.assertIn("inside the three-week tripwire", out)
        counts = self.host.dense_counts
        hour = dense.projection({**counts, "dense_seconds": counts["dense_hour_seconds"],
                                 "dense_tokens": counts["dense_hour_tokens"], "dense_copied": 0,
                                 "dense_embedded": counts["dense_hour_written"],
                                 "dense_scope": counts["dense_total"]}, NOW)
        assert hour is not None
        days, seconds = divmod(hour.generation_seconds, 86400)
        first_hour = next(line for line in out.splitlines() if "first-hour: ok" in line)
        self.assertIn(f"projected at {int(days)} d {seconds / 3600:.1f} h, finish {hour.finish.isoformat()}", first_hour)
        live = dense.projection({**counts, "dense_scope": 100}, NOW)
        assert live is not None
        heartbeat = next(line for line in out.splitlines() if "embedding, chunks written" in line)
        self.assertIn(f"{live.tokens_per_second:.1f} tok/s, projected finish {live.finish.isoformat()}", heartbeat)
        self.assertEqual(self.time, 121)

    def test_dense_no_projection_prints_counts_without_rate_or_finish(self) -> None:
        self.host.dense_pending_polls = 61
        self.host.dense_counts.update(dense_copied=0, dense_embedded=0, dense_seconds=60, dense_tokens=0)
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        heartbeat = next(line for line in out.splitlines() if "embedding, chunks written" in line)
        self.assertIn("chunks written 0 of 11 (present 3, copied 0, embedded 0), tokens 0", heartbeat)
        self.assertNotIn("tok/s", heartbeat)
        self.assertNotIn("projected finish", heartbeat)

    def test_limited_built_row_and_past_tripwire_first_hour(self) -> None:
        self.host.dense_finish_stamp = None
        self.host.dense_counts.update(dense_limit=2, dense_hour_seconds=3600, dense_hour_tokens=120,
                                     dense_hour_written=1, dense_total=dense.TRIPWIRE_DAYS * 24 + 1)
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("limited to 2 documents, chunks written 8 of 11; dense half not built: run", out)
        self.assertIn(report.command("index build caselaw"), out)
        self.assertIn("past the three-week tripwire", out)
        row = next(iter(self.host.generations.values()))
        self.assertIsNone(row.dense_built_at)
        self.assertEqual(row.state, "building")

    def test_dense_success_without_timestamp_or_limit_refuses_built(self) -> None:
        self.host.dense_finish_stamp = None
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("built: refuse", out)
        self.assertIn("no recorded dense_built_at", out)
        self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), out)

    def test_dense_failure_uses_its_fix_and_interrupt_leaves_the_worker_running(self) -> None:
        for reason, service in (("store", QDRANT_SERVICE_NAME), ("embed", EMBED.service_name), ("local", WORKER_SERVICE_NAME)):
            with self.subTest(reason=reason):
                self.host.dense_failure_reason = reason
                code, out, err = self.run_build()
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn("dense: refuse", out)
                self.assertIn(stack.logs_fix(RENDERED, service), out)
                self.assertNotIn("built:", out)
                self.assertIsNone(self.host.lock_holder)
        self.host.dense_failure_reason = None
        self.host.interrupt_phase = "dense"
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("interrupted; the embed continues in the worker", out)
        self.assertIn(report.command("index build caselaw"), out)
        self.assertIsNotNone(self.host.dense_live_id)
        self.assertIsNone(self.host.lock_holder)

    def test_unregistered_pipeline_and_unknown_court_refuse(self) -> None:
        path = self.fixture.checkout / f"corpus/lockfiles/{self.fixture.labels[0]}.yaml"
        loaded = load_lockfile(path, known_sources={"example": replace(SOURCES[0], name="example")})
        assert loaded.lockfile is not None
        path.write_text(render_lockfile(replace(loaded.lockfile, pipeline="1000.0.0")))
        self.assertIn("not registered", self.run_build(self.fixture.labels[0])[2])
        pin = loaded.lockfile.sources["example"]
        path.write_text(render_lockfile(replace(loaded.lockfile, sources={"example": replace(pin, courts=("unknown1",))})))
        self.assertIn("missing from the court map", self.run_build(self.fixture.labels[0])[2])

    def test_missing_allowlist_models_lock_and_lockfile_directory_refuse(self) -> None:
        for relative in ("config/egress.yaml", "models.lock", "corpus/lockfiles"):
            with self.subTest(relative=relative):
                path = self.fixture.checkout / relative
                saved = path.with_name(path.name + ".saved")
                path.rename(saved)
                try:
                    code, out, err = self.run_build()
                    self.assertEqual((code, out), (1, ""))
                    self.assertIn("Fix:", err)
                    self.assertEqual(self.host.deferred, [])
                    self.assertIsNone(self.host.lock_holder)
                finally:
                    saved.rename(path)

    def test_corrupt_sidecar_and_unavailable_record_read_refuse(self) -> None:
        label = self.fixture.labels[0]
        path = self.fixture.checkout / f"corpus/lockfiles/{label}/example.sha256"
        data = path.read_bytes()
        path.write_text("visibly fictitious corrupt sidecar\n")
        self.assertIn("corpus lockfiles", self.run_build(label)[2])
        path.write_bytes(data)
        original = self.host.run

        def failed_record(argv: Command, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "ORDER BY c.label" in (kwargs.get("input") or ""):
                return subprocess.CompletedProcess(list(argv), 127, "", "private diagnostic")
            return original(argv, **kwargs)

        self.host.run = failed_record  # type: ignore[method-assign]
        code, out, err = self.run_build(label)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Restore Docker Compose and psql", err)
        self.assertNotIn("private diagnostic", err)
        self.assertIsNone(self.host.lock_holder)

    def test_unknown_site_profile_refuses(self) -> None:
        self.host.site_text = (ROOT / "config/site.example.yaml").read_text() + "\nhardware_profile: 1000x96v-256d\n"
        code, out, err = self.run_build()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("is not in models.lock", err)
        self.assertEqual(self.host.deferred, [])

    def test_lock_access_failure_and_unexpected_exception_release_claims(self) -> None:
        original = self.host.take_lock

        def inaccessible(path: PathLike, record: str) -> str | None:
            raise PermissionError("fictitious inaccessible lock")

        self.host.take_lock = inaccessible  # type: ignore[method-assign]
        code, out, err = self.run_build()
        self.assertEqual((code, out), (1, ""))
        self.assertIn("lock could not be taken", err)
        self.host.take_lock = original  # type: ignore[method-assign]
        original_read = self.host.read_text

        def defect(path: PathLike, *, encoding: str = "utf-8") -> str:
            if str(path).endswith("models.lock"):
                raise RuntimeError("fictitious defect")
            return original_read(path, encoding=encoding)

        self.host.read_text = defect  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            self.run_build()
        self.assertIsNone(self.host.lock_holder)
