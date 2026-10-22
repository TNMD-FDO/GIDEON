"""The index build command over fictitious lockfiles and a recorded queue.

test_corpus_install's FakeHost answers the stage counts' write and read. Production
runs cover profile and scope; guard and reserve figures, skips, and refusals before
any job or report; service checks, defers and rejoins, clustering, leveled counts,
heartbeats, limits, treatment, failures, locks, and interrupts; footprint measure,
write, and recovery; report gates, whole-build cases, failures, and re-run append; and
the smoke row's place, refusal, and absence from a sample build. Sample runs cover the
stage order, each statement's directory, the counts read against production's, the
given gold recorded before any job and a refill's union, the failed write's refusal,
the guard refusal, the reserve skip and judgment, reported and unreported refusals,
and interrupts.
"""

import argparse
import contextlib
import copy
import io
import json
import os
import shlex
import shutil
import subprocess
import unittest
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import test_corpus_install
import yaml  # type: ignore[import-untyped]
from test_corpus_install import NOW, RENDERED, ROOT, SITE_PATH, FakeHost
from test_engine_verify import models_text
from test_index_record import row_for
from test_models_lock import VALID, VALID_WITH_SPACE

from gideon.cli import build_parser
from gideon.host import (
    backuplock,
    caselaw,
    models,
    report,
    secrets,
    stack,
    staging,
    worker,
)
from gideon.host.corpus.lockfile import load_lockfile, render_lockfile
from gideon.host.corpus.record import database_fix
from gideon.host.corpus.sources import SOURCES, SourceDefinition
from gideon.host.index import (
    build,
    buildreport,
    dense,
    generations,
    guards,
    lexical,
    reserve,
    sample,
    smoke,
    treatment,
)
from gideon.host.index.record import GenerationRow, PromotedRow
from gideon.host.index.sample import GoldDocument, SampleInputs
from gideon.host.render.ci import CI_QDRANT_DATA_ROOT, CI_ROOT, CI_SECRETS_DIR
from gideon.host.render.engine import EMBED
from gideon.host.render.opensearch import OPENSEARCH_SERVICE_NAME
from gideon.host.render.qdrant import (
    QDRANT_DATA_ROOT,
    QDRANT_SECRET_NAME,
    QDRANT_SERVICE_NAME,
)
from gideon.host.render.worker import (
    AGREEMENT_RECORD_NAME,
    CLUSTER_BUDGET_SECONDS,
    DENSE_LOCK_PREFIX,
    DENSE_TASK,
    DUPES_LOCK_PREFIX,
    DUPES_TASK,
    LEXICAL_TASK,
    OVERRULED_SOURCE,
    WORKER_SERVICE_NAME,
)
from gideon.host.sysio import Command, PathLike


class Build(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_corpus_install.Install()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.host: FakeHost = self.fixture.host
        for label in self.fixture.labels:
            self.fixture._hold_record("cut", label)
            self.fixture.seed_report_work(label)
        document = yaml.safe_load(models_text())
        profile = document["profiles"][document["reference"]]
        profile["memory"]["qdrant"] = {"gb": 7}
        profile["chunk_guards"] = {
            "serving_ceiling_million_chunks": 5, "build_refusal_million_chunks": 3,
            "resident_bytes_per_chunk": 1300,
        }
        (self.fixture.checkout / "models.lock").write_text(yaml.safe_dump(document, sort_keys=False))
        self.time = 0.0

    def run_build(
        self, label: str | None = None, *, sample: SampleInputs | None = None,
        collection: str = "caselaw", sleep: Callable[[float], None] | None = None,
        dense_limit: int | None = None,
        sources: tuple[SourceDefinition, ...] | None = None,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        self.host.current_label = label or self.fixture.labels[-1]
        self.host.identity_checked = False

        def tick(seconds: float) -> None:
            self.time += seconds

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build.run_index_build(
                argparse.Namespace(collection=collection, lockfile=label, sample=sample is not None),
                host=self.host, rendered_dir=RENDERED, checkout=self.fixture.checkout,
                work_root=self.fixture.work,
                sources=sources if sources is not None else (self.fixture.source,), clock=lambda: NOW,
                monotonic=lambda: self.time, sleep=sleep or tick, dense_limit=dense_limit,
                sample=sample,
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

    def test_finish_generation_runs_treatment_and_report_alone_over_a_built_row(self) -> None:
        label = self.fixture.labels[0]
        self.assertEqual(self.run_build(label)[0], 0)
        row = next(iter(self.host.generations.values()))
        self.assertEqual(row.state, "built")
        target = build.embed_target(self.host, self.fixture.checkout, SITE_PATH, build.COMMAND_PATH)
        self.assertNotIsInstance(target, report.Problem)
        assert not isinstance(target, report.Problem)
        inputs = dense.DenseInputs(**asdict(self.inputs(label)), served_model=target.served_name,
                                   dimensions=target.dimensions)
        deferred = copy.deepcopy(self.host.deferred)
        jobs = copy.deepcopy(self.host.jobs)
        materializations, reports = len(self.host.treatment_results), len(self.host.reports)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build.finish_generation(
                self.host, RENDERED, inputs, build.COMMAND_PATH, started_at=0.0,
                monotonic=lambda: 5.0, active_stage=["treatment"], treatment_snapshot=None,
                work_root=self.fixture.work,
            )
        self.assertEqual((code, err.getvalue()), (0, ""), out.getvalue())
        self.assertEqual([line.split(":", 1)[0] for line in out.getvalue().splitlines()], ["treatment", "report"])
        self.assertEqual(self.host.deferred, deferred)
        self.assertEqual(self.host.jobs, jobs)
        self.assertEqual(self.host.generations[row.gen_id], row)
        self.assertEqual(len(self.host.treatment_results), materializations + 1)
        self.assertEqual(len(self.host.reports), reports + 1)
        self.assertIsNone(self.host.reports[-1]["failed_stage"])
        self.assertEqual(self.host.reports[-1]["treatment"], self.host.treatment_results[-1].to_json())
        self.assertFalse(self.host.reports[-1]["wall_clock"]["whole_build"])
        self.assertEqual(self.host.reports[-1]["seconds"], 5)

    def test_smoke_record_evidence_prints_between_preconditions_and_generation(self) -> None:
        """A passing sample run licenses the build before its first write."""

        label = self.fixture.labels[0]
        inputs = self.inputs(label)
        evidence = self.host.smoke_row
        assert evidence is not None
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        stages = [line.split(":", 1)[0] for line in out.splitlines()]
        self.assertEqual(stages[:3], ["preconditions", "smoke", "generation"])
        self.assertEqual(stages.count("smoke"), 1)
        self.assertIn(
            f"smoke: ok — lockfile {inputs.label}, pipeline {inputs.pipeline}, "
            f"embedding space {inputs.embedding_space}: run {evidence['run_id']} "
            f"({evidence['stack']}, {evidence['kind']}, finished {NOW:%Y-%m-%dT%H:%M}Z)", out,
        )
        statements = self.host.statements
        read = next(index for index, sql in enumerate(statements) if "FROM public.eval_runs" in sql)
        write = next(index for index, sql in enumerate(statements) if "DO $generation$" in sql)
        self.assertLess(read, write)
        self.assertIn(worker.bind("v_label", label), statements[read])
        self.assertIn(worker.bind("v_sample", smoke.expected_sample(inputs)), statements[read])
        self.assertTrue(self.host.deferred)

    def test_missing_smoke_refuses_before_any_write_or_job_and_releases_the_lock(self) -> None:
        """Missing evidence names its inputs and recovery, leaving no generation."""

        self.host.smoke_row = None
        label = self.fixture.labels[0]
        inputs = self.inputs(label)
        corpus_rows = copy.deepcopy(self.host.record_rows)
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (1, ""), out)
        self.assertEqual([line.split(":", 1)[0] for line in out.splitlines()], ["preconditions", "smoke"])
        self.assertIn(
            f"smoke: refuse — no passing smoke run for lockfile {label}, pipeline {inputs.pipeline}, "
            f"embedding space {inputs.embedding_space}: none names generation {smoke.expected_sample(inputs)}", out,
        )
        self.assertIn(report.command(f"index build caselaw --sample --lockfile {label}"), out)
        self.assertIn(report.command(
            f"eval run --slice smoke --stack ci --kind smoke --generation {smoke.expected_sample(inputs)}",
        ), out)
        self.assertIn(f"then retry {report.command(f'index build caselaw --lockfile {label}')}", out)
        self.assertEqual((self.host.generations, self.host.jobs, self.host.reports), ({}, {}, []))
        self.assertEqual(self.host.record_rows, corpus_rows)
        self.assertFalse(any("DO $generation$" in sql for sql in self.host.statements))
        self.assertIsNone(self.host.lock_holder)
        self.assertEqual(self.host.lock_releases, 1)

    def test_smoke_reader_failure_refuses_before_generation_and_releases_the_lock(self) -> None:
        self.host.fail_phase = "smoke"
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("smoke: refuse — smoke reader failed: exit 3", out)
        self.assertIn(stack.logs_fix(RENDERED, "postgres"), out)
        self.assertNotIn("private diagnostic", out)
        self.assertEqual((self.host.generations, self.host.jobs, self.host.reports), ({}, {}, []))
        self.assertIsNone(self.host.lock_holder)
        self.assertEqual(self.host.lock_releases, 1)

    def sample_inputs(self, gold: tuple[GoldDocument, ...] = ()) -> SampleInputs:
        return SampleInputs(gold, sample.SAMPLE_FILL_PER_MILLION, "eval-v1000")

    def assert_sample_fix(self, output: str, label: str) -> None:
        """Every refusal includes a complete sample retry that the CLI accepts."""

        retry = report.command(f"index build caselaw --sample --lockfile {label}")
        fixes = [line.split("Fix:", 1)[1] for line in output.splitlines() if "Fix:" in line]
        self.assertTrue(fixes, output)
        for fix in fixes:
            self.assertIn(retry, fix)
            words = shlex.split(retry)
            args = build_parser().parse_args(words[words.index("index"):])
            self.assertTrue(args.sample)
            self.assertEqual((args.collection, args.lockfile), ("caselaw", label))

    def test_sample_build_holds_the_engine_and_uses_the_siblings_record_and_stores(self) -> None:
        """The sibling builds from production's corpus without writing its record."""

        label = self.fixture.labels[0]
        base = self.inputs(label)
        selection = self.sample_inputs()
        self.host.smoke_row = None
        self.host.caselaw_anchor_counts = {
            court: {"ready": 1, "anchored": 1, "by_text_source": {
                "xml_harvard": {"documents": 1, "with_anchors": 1, "chars": 10, "anchored_chars": 10},
            }} for court in base.courts
        }
        production_rows = copy.deepcopy(self.host.record_rows)
        code, out, err = self.run_build(label, sample=selection)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.host.lock_paths, [backuplock.ENGINE_LOCK.path])
        self.assertEqual((self.host.engine_holder, self.host.engine_releases, self.host.lock_releases), (None, 1, 0))
        self.assertEqual(self.host.record_rows, production_rows)
        self.assertIn(label, self.host.sibling_lockfiles)
        self.assertEqual(self.host.sibling_lockfiles[label]["state"], production_rows[label]["state"])
        self.assertEqual(self.host.sibling_lockfiles[label]["installed_at"], production_rows[label]["installed_at"])
        name = sample.sample_name(base.name)
        row = self.host.generations[name]
        self.assertEqual(row.inputs_hash, sample.sample_inputs_hash(base.inputs_hash, label, selection.fill_per_million))
        self.assertEqual([args["sample"] for args in self.host.deferred], [sample.arguments(selection)] * 3)
        self.assertEqual([args["name"] for args in self.host.deferred], [name] * 3)
        self.assertEqual(list(self.host.task_by_job.values()), [DUPES_TASK, LEXICAL_TASK, DENSE_TASK])
        stages = ("preconditions", "lockfile", "generation", "guard", "reserve", "cluster", "build", "counts",
                  "dense", "gold", "built", "treatment", "report")
        self.assertEqual(sorted(stages, key=lambda stage: out.index(f"{stage}: ok")), list(stages))
        self.assertEqual([line for line in out.splitlines()
                          if line.split(":")[0] == "smoke"], [])
        self.assertEqual(self.host.treatment_payloads[0]["gen_id"], name)
        self.assertEqual(self.host.treatment_reads[0]["doc_ids"], [])
        self.assertEqual(self.host.reports[0]["gen_id"], name)
        self.assertEqual(self.host.reports[0]["treatment"], self.host.treatment_results[0].to_json())
        self.assertTrue(self.host.reports[0]["wall_clock"]["whole_build"])
        self.assertEqual(self.host.reports[0]["wall_clock"]["run_seconds"], self.time)
        self.assertEqual(self.host.reports[0]["gates"]["cluster"], {
            "seconds": row.counts["cluster_seconds"], "budget": CLUSTER_BUDGET_SECONDS,
            "reused": False, "pass": True,
        })
        self.assertEqual(tuple(self.host.reports[0]["courts"]), tuple(sorted(base.courts)))
        for court in base.courts:
            self.assertEqual(self.host.reports[0]["courts"][court]["anchors"], self.host.caselaw_anchor_counts[court])
            self.assertIsNotNone(self.host.reports[0]["courts"][court]["agreement"])
            self.assertEqual(self.host.reports[0]["gates"]["accounted"][court]["staged"],
                             self.host.stage_counts_rows[(label, self.fixture.source.name, court)]["opinions"])
        self.assertIn("gold 0, present 0", out)
        self.assertIn(f"eval set {selection.set_version}", out)
        state_commands = [argv for argv, sql, _ in self.host.executions if sql is None]
        self.assertEqual(state_commands, [
            *[stack.compose_argv(CI_ROOT, "ps", "--all", "--format", "json")] * 3,
            stack.compose_argv(RENDERED, "ps", "--all", "--format", "json"),
            list(reserve.DATA_DF_ARGV),
            ["du", "-sb", f"{CI_QDRANT_DATA_ROOT}/collections/{name}"],
        ])
        for argv, sql, holder_text in self.host.executions:
            holder = backuplock.parse(holder_text or "")
            self.assertIsNotNone(holder)
            assert holder is not None
            self.assertEqual(holder.command, f"gideon index build caselaw --sample --lockfile {label}")
            if sql is None:
                continue
            self.assertNotIn(name, " ".join(argv))
        self.assertEqual(len(self.host.statement_directories),
                         sum(sql is not None and "psql" in argv for argv, sql, _ in self.host.executions))
        for directory, sql in self.host.statement_directories:
            production_read = (
                "ORDER BY c.label" in sql or "WITH RECURSIVE chain AS" in sql
                or "FROM public.stage_counts" in sql
                or any(statement in sql for statement in (
                    guards.CHUNK_PROJECTION_SQL, caselaw.CASELAW_COUNTS_SQL,
                    caselaw.ANCHOR_COUNTS_SQL, treatment.READ_SQL,
                ))
            )
            self.assertEqual(directory, RENDERED if production_read else str(CI_ROOT))
        reads = [(directory, sql) for directory, sql in self.host.statement_directories
                 if "FROM public.stage_counts" in sql]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0][0], RENDERED)
        self.assertIn(worker.bind("v_label", label), reads[0][1])

    def test_sample_records_sorted_gold_before_jobs_and_a_larger_refill_adds_to_it(self) -> None:
        label = self.fixture.labels[0]
        name = sample.sample_name(self.inputs(label).name)
        gold = tuple(GoldDocument(sample.source_id("example", number), None) for number in (90, 10))
        selections = (
            self.sample_inputs(gold),
            self.sample_inputs((*gold, GoldDocument(sample.source_id("example", 20), "b" * 64))),
        )
        given: set[str] = set()
        for selection in selections:
            with self.subTest(gold=selection.gold):
                execution_count = len(self.host.executions)
                statement_count = len(self.host.statement_directories)
                deferred_count = len(self.host.deferred)
                code, out, err = self.run_build(label, sample=selection)
                self.assertEqual((code, err), (0, ""), out)
                identifiers = sorted(document.source_id for document in selection.gold)
                given.update(identifiers)
                self.assertEqual(self.host.given_gold, {name: given})
                writes = [(directory, sql) for directory, sql in self.host.statement_directories[statement_count:]
                          if "public.generation_gold" in sql]
                self.assertEqual(len(writes), 1)
                directory, sql = writes[0]
                self.assertEqual(directory, str(CI_ROOT))
                self.assertEqual(self.host._bound(sql, "v_name"), name)
                self.assertEqual(json.loads(self.host._bound(sql, "v_source_ids")), identifiers)
                executions = self.host.executions[execution_count:]
                write = next(index for index, (_, statement, _) in enumerate(executions) if statement == sql)
                argv = executions[write][0]
                self.assertEqual(argv, worker.psql_argv(CI_ROOT))
                for value in (name, *identifiers, "INSERT", "v_source_ids"):
                    self.assertNotIn(value, " ".join(argv))
                first_job = next(index for index, (_, statement, _) in enumerate(executions)
                                 if statement and "procrastinate_defer_jobs_v1" in statement)
                self.assertLess(write, first_job)
                generation = next(index for index, (_, statement, _) in enumerate(executions)
                                  if statement and "DO $generation$" in statement)
                self.assertLess(generation, write)
                self.assertEqual(len(self.host.deferred[deferred_count:]), 3)
                lines = [line for line in out.splitlines() if line.startswith("generation:")]
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].endswith(f"; gold {len(selection.gold)} recorded"), lines[0])

    def test_sample_gold_write_failure_refuses_generation_without_jobs_or_report_and_recovers(self) -> None:
        label = self.fixture.labels[0]
        name = sample.sample_name(self.inputs(label).name)
        selection = self.sample_inputs((GoldDocument(sample.source_id("example", 17), None),))
        self.host.fail_phase = "gold-write"
        code, out, err = self.run_build(label, sample=selection)
        self.assertEqual((code, err), (1, ""), out)
        command_path = f"index build caselaw --sample --lockfile {label}"
        self.assertEqual(out.splitlines()[-1], report.stage_line(report.StageResult(
            "generation", False, "generation gold write failed (exit 3)", database_fix(CI_ROOT, command_path),
        )))
        self.assertEqual([line.split(":", 1)[0] for line in out.splitlines()],
                         ["preconditions", "lockfile", "generation"])
        self.assertNotIn("generation: ok", out)
        self.assertNotIn("report:", out)
        self.assertEqual((self.host.given_gold, self.host.deferred, self.host.jobs, self.host.reports), ({}, [], {}, []))
        self.assertEqual(self.host.generations[name].state, "building")
        self.assertIsNone(self.host.engine_holder)
        self.assertEqual(self.host.engine_releases, 1)
        self.assert_sample_fix(out, label)

        self.host.fail_phase = None
        code, out, err = self.run_build(label, sample=selection)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.host.given_gold[name], {document.source_id for document in selection.gold})
        self.assertEqual(len(self.host.deferred), 3)
        self.assertEqual(len(self.host.reports), 1)
        self.assertEqual(self.host.generations[name].state, "built")
        self.assertIn("report: ok", out)
        self.assertIsNone(self.host.engine_holder)
        self.assertEqual(self.host.engine_releases, 2)

    def test_sample_refill_interrupted_at_cluster_keeps_the_union_of_given_gold(self) -> None:
        label = self.fixture.labels[0]
        name = sample.sample_name(self.inputs(label).name)
        earlier = self.sample_inputs((GoldDocument(sample.source_id("example", 90), None),))
        self.assertEqual(self.run_build(label, sample=earlier)[0], 0)
        reports = copy.deepcopy(self.host.reports)
        selection = self.sample_inputs((GoldDocument(sample.source_id("example", 20), "b" * 64),))
        self.host.interrupt_phase = "cluster"
        code, out, err = self.run_build(label, sample=selection)
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("cluster: refuse — interrupted; the clustering continues in the worker", out)
        self.assertIn(f"generation: ok — {name}: built, found; gold {len(selection.gold)} recorded", out)
        self.assertEqual(self.host.given_gold[name], {document.source_id for document in (*earlier.gold, *selection.gold)})
        self.assertIsNotNone(self.host.cluster_live_id)
        self.assertEqual(self.host.reports, reports)
        self.assertNotIn("report:", out)
        self.assertIsNone(self.host.engine_holder)
        self.assertEqual(self.host.engine_releases, 2)

    def test_sample_guard_refuses_the_whole_scope_before_any_job_or_report(self) -> None:
        label = self.fixture.labels[0]
        inputs = self.inputs(label)
        profile_guards = build.chunk_guards(self.host, self.fixture.checkout, SITE_PATH, build.COMMAND_PATH)
        assert isinstance(profile_guards, models.ChunkGuards)
        chunks = profile_guards.build_refusal_chunks + 1
        self.host.projection_counts = {"ready": 1, "chunked": 1, "chunks": chunks}
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn(f"guard: refuse — {sample.sample_name(inputs.name)}", out)
        self.assertIn(f"projected chunks {chunks}", out)
        self.assertIn(f"build refusal {profile_guards.build_refusal_chunks}", out)
        self.assertIn("counted none = 0", out)
        self.assertEqual((self.host.deferred, self.host.jobs, self.host.reports), ([], {}, []))
        self.assertNotIn("reserve:", out)
        self.assertNotIn("report:", out)
        directory, sql = next((directory, sql) for directory, sql in self.host.statement_directories
                              if guards.CHUNK_PROJECTION_SQL in sql)
        self.assertEqual(directory, RENDERED)
        self.assertEqual(self.host._bound(sql, "v_courts"), "{" + ",".join(inputs.courts) + "}")
        self.assertEqual(self.host._bound(sql, "v_pipeline"), inputs.pipeline)
        self.assertIsNone(self.host.engine_holder)
        self.assert_sample_fix(out, label)

    def test_sample_reserve_skips_without_a_live_sibling_and_judges_a_live_one(self) -> None:
        label = self.fixture.labels[0]
        name = sample.sample_name(self.inputs(label).name)
        self.assertEqual(self.run_build(label, sample=self.sample_inputs())[0], 0)
        self.assertEqual(len(self.host.reports), 1)
        # The first run wrote a whole build; a refill reports only this run's time.
        self.assertTrue(self.host.reports[0]["wall_clock"]["whole_build"])
        for live_bytes in (0, self.host.free_bytes // 2, self.host.free_bytes):
            with self.subTest(live_bytes=live_bytes):
                self.host.promoted = () if live_bytes == 0 else (PromotedRow(name, "caselaw", live_bytes),)
                self.host.statement_directories.clear()
                self.host.deferred.clear()
                self.host.reports.clear()
                code, out, err = self.run_build(label, sample=self.sample_inputs())
                verdict = reserve.judge(self.host.free_bytes, live_bytes)
                self.assertEqual((code, err), (0 if verdict.build_cleared else 1, ""), out)
                directories = [directory for directory, sql in self.host.statement_directories
                               if "WHERE state = 'live'" in sql]
                self.assertEqual(directories, [str(CI_ROOT)])
                self.assertIn(list(reserve.DATA_DF_ARGV), self.host.calls)
                if live_bytes == 0:
                    self.assertIn("reserve: ok — skipped: no promoted generation for caselaw", out)
                else:
                    self.assertIn(reserve.describe(verdict), out)
                if verdict.build_cleared:
                    self.assertEqual(len(self.host.deferred), 3)
                    self.assertEqual(len(self.host.reports), 1)
                    self.assertFalse(self.host.reports[0]["wall_clock"]["whole_build"])
                else:
                    self.assertIn("reserve: refuse", out)
                    self.assertEqual((self.host.deferred, self.host.reports), ([], []))
                    self.assertNotIn("report:", out)
                    self.assert_sample_fix(out, label)
                self.assertIsNone(self.host.engine_holder)

    def test_sample_treatment_is_bounded_by_the_whole_manifest_on_the_sibling(self) -> None:
        label = self.fixture.labels[0]
        self.host.manifest_rows = (
            (sample.source_id("example", 90), "b" * 64, "d" * 64),
            (sample.source_id("example", 10), "a" * 64, "c" * 64),
        )
        selection = self.sample_inputs((GoldDocument(self.host.manifest_rows[0][0], "b" * 64),))
        code, out, err = self.run_build(label, sample=selection)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.host.treatment_reads[0]["doc_ids"],
                         [row[2] for row in sorted(self.host.manifest_rows)])
        manifest = [(directory, sql) for directory, sql in self.host.statement_directories
                    if "canonical_text_sha256, doc_id" in sql]
        self.assertEqual(len(manifest), 1)
        directory, sql = manifest[0]
        self.assertEqual(directory, str(CI_ROOT))
        self.assertEqual(self.host._bound(sql, "v_name"), sample.sample_name(self.inputs(label).name))
        self.assertLess(self.host.statements.index(sql),
                        next(index for index, statement in enumerate(self.host.statements)
                             if treatment.READ_SQL in statement))

    def test_sample_manifest_failure_refuses_treatment_and_reports_its_reader_fix(self) -> None:
        label = self.fixture.labels[0]
        self.host.fail_phase = "treatment-manifest"
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("treatment: refuse — generation manifest read failed (exit 3)", out)
        self.assertIn(stack.logs_fix(CI_ROOT, "postgres"), out)
        self.assertEqual((self.host.treatment_reads, self.host.treatment_payloads), ([], []))
        self.assertEqual(self.host.reports[-1]["failed_stage"], "treatment")
        self.assertIsNone(self.host.reports[-1]["treatment"])
        self.assertIsNone(self.host.engine_holder)
        self.assert_sample_fix(out, label)

    def test_sample_cluster_build_counts_dense_and_built_failures_report_the_failed_stage(self) -> None:
        label = self.fixture.labels[0]
        for stage in ("cluster", "build", "counts", "dense", "built"):
            with self.subTest(stage=stage):
                self.host.generations.clear()
                self.host.reports.clear()
                self.host.cluster_failure_reason = "cluster-budget" if stage == "cluster" else None
                self.host.failure_reason = "store" if stage == "build" else None
                self.host.finish_stamp = None if stage == "counts" else NOW.isoformat()
                self.host.dense_failure_reason = "store" if stage == "dense" else None
                self.host.dense_finish_stamp = None if stage == "built" else NOW.isoformat()
                code, out, err = self.run_build(label, sample=self.sample_inputs())
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn(f"{stage}: refuse", out)
                self.assertIn("report: ok", out)
                self.assertEqual(len(self.host.reports), 1)
                payload = self.host.reports[0]
                self.assertEqual(payload["gen_id"], sample.sample_name(self.inputs(label).name))
                self.assertEqual(payload["failed_stage"], stage)
                self.assertEqual(payload["failure_reason"],
                                 "cluster-budget" if stage == "cluster" else "store" if stage in {"build", "dense"} else None)
                if stage == "cluster":
                    self.assertNotIn("build: ok", out)
                    self.assertFalse(payload["gates"]["cluster"]["pass"])
                self.assertFalse(payload["wall_clock"]["whole_build"])
                self.assertIsNone(payload["treatment"])
                self.assertEqual(self.host.treatment_payloads, [])
                self.assertIsNone(self.host.engine_holder)
                self.assert_sample_fix(out, label)

    def test_sample_gold_read_failure_writes_no_report_and_a_rerun_reaches_gold(self) -> None:
        label = self.fixture.labels[0]
        self.host.fail_phase = "gold"
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("gold: refuse — generation manifest read failed (exit 3)", out)
        self.assertIn(stack.logs_fix(CI_ROOT, "postgres"), out)
        self.assertEqual((self.host.reports, self.host.treatment_payloads), ([], []))
        self.assertNotIn("report:", out)
        self.assertNotIn("built:", out)
        self.assertIsNone(self.host.engine_holder)
        self.assert_sample_fix(out, label)
        self.host.fail_phase = None
        count = len(self.host.deferred)
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotIn("rejoined", out)
        self.assertIn("cluster: ok", out)
        self.assertEqual(len(self.host.deferred[count:]), 3)
        self.assertIn("gold: ok", out)
        self.assertIn("report: ok", out)
        self.assertEqual(len(self.host.reports), 1)
        self.assertFalse(self.host.reports[0]["wall_clock"]["whole_build"])

    def test_production_treatment_and_report_reads_use_only_the_rendered_tree(self) -> None:
        code, out, err = self.run_build(self.fixture.labels[0])
        self.assertEqual((code, err), (0, ""), out)
        for statement in (treatment.READ_SQL, treatment.WRITE_SQL,
                          caselaw.CASELAW_COUNTS_SQL, caselaw.ANCHOR_COUNTS_SQL,
                          buildreport.WRITE_REPORT_SQL):
            directories = [directory for directory, sql in self.host.statement_directories if statement in sql]
            self.assertTrue(directories)
            self.assertEqual(set(directories), {RENDERED})
        self.assertIsNone(self.host.treatment_reads[0]["doc_ids"])
        self.assertFalse(any("public.generation_documents" in sql for _, sql in self.host.statement_directories))
        self.assertFalse(any("public.generation_gold" in sql for _, sql in self.host.statement_directories))
        self.assertEqual(self.host.given_gold, {})

    def test_sample_rejoins_then_runs_all_three_jobs_with_the_current_selection(self) -> None:
        """Rejoined jobs may carry earlier gold, so each runs again with this selection."""

        label = self.fixture.labels[0]
        self.assertEqual(self.run_build(label, sample=self.sample_inputs())[0], 0)
        cluster_job = next(job for job, task in self.host.task_by_job.items() if task == DUPES_TASK)
        lexical_job = next(job for job, task in self.host.task_by_job.items() if task == LEXICAL_TASK)
        dense_job = next(job for job, task in self.host.task_by_job.items() if task == DENSE_TASK)
        self.host.live_id, self.host.dense_live_id = lexical_job, dense_job
        self.host.cluster_live_id = cluster_job
        selection = self.sample_inputs((GoldDocument(sample.source_id("example", 7), "b" * 64),))
        count = len(self.host.deferred)
        execution_count = len(self.host.executions)

        code, out, err = self.run_build(label, sample=selection)

        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"rejoined job {cluster_job}", out)
        self.assertIn(f"rejoined job {lexical_job}", out)
        self.assertIn(f"rejoined job {dense_job}", out)
        deferred = self.host.deferred[count:]
        self.assertEqual(len(deferred), 3)
        self.assertEqual([args["sample"] for args in deferred], [sample.arguments(selection)] * 3)
        new_cluster_job = max(job for job, task in self.host.task_by_job.items() if task == DUPES_TASK)
        self.assertNotEqual(new_cluster_job, cluster_job)
        self.assertEqual(self.host.jobs[cluster_job]["sample"], sample.arguments(self.sample_inputs()))
        self.assertEqual(self.host.jobs[new_cluster_job]["sample"], sample.arguments(selection))
        self.assertLess(out.index(f"rejoined job {cluster_job}"), out.index(f"deferred job {new_cluster_job}"))
        self.assertLess(out.index(f"deferred job {new_cluster_job}"), out.index(f"rejoined job {lexical_job}"))
        self.assertLess(out.index(f"rejoined job {lexical_job}"), out.index("counts: ok"))
        self.assertLess(out.index(f"rejoined job {dense_job}"), out.index("gold: ok"))
        writes = [sql for sql in self.host.statements if "public.generation_gold" in sql]
        self.assertEqual(len(writes), 2)
        self.assertEqual(json.loads(self.host._bound(writes[0], "v_source_ids")), [])
        identifiers = sorted(document.source_id for document in selection.gold)
        self.assertEqual(json.loads(self.host._bound(writes[1], "v_source_ids")), identifiers)
        name = sample.sample_name(self.inputs(label).name)
        self.assertEqual(self.host._bound(writes[1], "v_name"), name)
        self.assertEqual(self.host.given_gold[name], set(identifiers))
        executions = self.host.executions[execution_count:]
        write = next(index for index, (_, sql, _) in enumerate(executions) if sql == writes[1])
        rejoin = next(index for index, (_, sql, _) in enumerate(executions)
                      if sql and worker.bind("v_job_id", str(cluster_job)) in sql)
        self.assertLess(write, rejoin)

    def test_sample_gold_reports_present_absent_and_changed_ids_in_sorted_order(self) -> None:
        identifiers = {number: sample.source_id("example", number) for number in (90, 80, 70, 60, 20, 10)}
        gold = tuple(GoldDocument(identifier, None if number == 10 else "a" * 64)
                     for number, identifier in identifiers.items())
        self.host.manifest_hashes = {
            identifiers[80]: "b" * 64, identifiers[70]: "a" * 64,
            identifiers[20]: "c" * 64, identifiers[10]: "d" * 64,
        }
        code, out, err = self.run_build(self.fixture.labels[0], sample=self.sample_inputs(gold))
        self.assertEqual((code, err), (0, ""), out)
        gold_line = next(line for line in out.splitlines() if line.startswith("gold:"))
        self.assertIn("gold 6, present 4", gold_line)
        self.assertIn(f"absent {identifiers[60]}, {identifiers[90]}", gold_line)
        self.assertIn(f"hash-changed {identifiers[20]}, {identifiers[80]}", gold_line)
        self.assertNotIn("refuse", out)
        self.assertNotIn("a" * 64, gold_line)
        sql = next(sql for _, sql, _ in self.host.executions if sql and "public.generation_documents" in sql)
        self.assertEqual(json.loads(self.host._bound(sql, "v_source_ids")), sorted(identifiers.values()))

    def test_sample_generation_with_another_hash_refuses_before_any_job(self) -> None:
        label = self.fixture.labels[0]
        selection = self.sample_inputs()
        self.assertEqual(self.run_build(label, sample=selection)[0], 0)
        name, row = next(iter(self.host.generations.items()))
        self.host.generations[name] = replace(row, inputs_hash="f" * 64)
        self.host.deferred.clear()
        code, out, err = self.run_build(label, sample=selection)
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("generation: refuse", out)
        self.assertIn("another inputs hash", out)
        self.assertIn("sudo python3 -m tools.cistack down --wipe", out)
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.generations[name].inputs_hash, "f" * 64)
        self.assertIsNone(self.host.engine_holder)
        self.assert_sample_fix(out, label)

    def test_sample_refills_a_live_generation_and_failures_leave_it_standing(self) -> None:
        label = self.fixture.labels[0]
        selection = self.sample_inputs()
        self.assertEqual(self.run_build(label, sample=selection)[0], 0)
        name, row = next(iter(self.host.generations.items()))
        promoted = replace(row, state="live", promoted_at=NOW.isoformat())
        self.host.generations[name] = promoted
        self.host.deferred.clear()
        selection = self.sample_inputs((GoldDocument(sample.source_id("example", 17), None),))
        code, out, err = self.run_build(label, sample=selection)
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotIn("nothing built", out)
        self.assertEqual(len(self.host.deferred), 3)
        self.assertEqual(self.host.generations[name].state, "live")
        self.assertEqual(self.host.given_gold[name], {document.source_id for document in selection.gold})
        self.assertIn(f"generation: ok — {name}: live, found; gold {len(selection.gold)} recorded", out)
        for attribute in ("failure_reason", "dense_failure_reason"):
            with self.subTest(job=attribute):
                self.host.generations[name] = promoted
                setattr(self.host, attribute, "store")
                code, out, err = self.run_build(label, sample=selection)
                setattr(self.host, attribute, None)
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn("sample refill failed; the promoted generation stands", out)
                self.assertIn(stack.logs_fix(CI_ROOT, WORKER_SERVICE_NAME), out)
                self.assertEqual(self.host.generations[name].state, "live")
                self.assertEqual(self.host.generations[name].promoted_at, promoted.promoted_at)
                self.assertIsNone(self.host.engine_holder)
                self.assert_sample_fix(out, label)

    def test_sample_refuses_a_held_engine_without_touching_the_sibling(self) -> None:
        label = self.fixture.labels[0]
        holder = backuplock.Record("gideon eval run --stack ci", os.getpid() + 1000, NOW)
        self.host.engine_holder = holder.to_json()
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, out), (1, ""))
        self.assertIn(holder.command, err)
        self.assertIn(str(holder.pid), err)
        self.assertIn("Wait", err)
        self.assertEqual(self.host.executions, [])
        self.assertEqual(self.host.sibling_lockfiles, {})
        self.assertEqual(self.host.engine_holder, holder.to_json())
        self.assertEqual(self.host.engine_releases, 0)
        self.assert_sample_fix(err, label)

    def test_sample_failures_release_the_engine_and_print_parseable_sample_fixes(self) -> None:
        label = self.fixture.labels[0]
        phases = ("copy-read", "lockfile", "generation-read", "generation", "gold-write",
                  "cluster-live-job", "cluster-defer", "cluster", "cluster-counts", "live-job", "defer", "build", "counts",
                  "dense-live-job", "dense-defer", "dense", "dense-counts", "gold")
        for phase in phases:
            with self.subTest(phase=phase):
                self.host.generations.clear()
                self.host.jobs.clear()
                self.host.live_id = self.host.dense_live_id = self.host.cluster_live_id = None
                self.host.active_task = LEXICAL_TASK
                self.host.fail_phase = phase
                before = self.host.engine_releases
                code, out, err = self.run_build(label, sample=self.sample_inputs())
                self.assertEqual(code, 1, out + err)
                self.assertIn("refuse", out + err)
                self.assertNotIn("private diagnostic", out + err)
                self.assertIsNone(self.host.engine_holder)
                self.assertEqual(self.host.engine_releases, before + 1)
                self.assert_sample_fix(out + err, label)

    def test_sample_interrupts_release_the_engine_at_each_stage(self) -> None:
        label = self.fixture.labels[0]
        for phase in ("preconditions", "lockfile", "generation-read", "generation", "gold-write", "guard", "reserve",
                      "cluster", "build", "counts", "dense", "gold", "treatment-manifest", "treatment-read", "treatment", "report"):
            with self.subTest(phase=phase):
                self.host.generations.clear()
                self.host.jobs.clear()
                self.host.live_id = self.host.dense_live_id = self.host.cluster_live_id = None
                self.host.active_task = LEXICAL_TASK
                self.host.interrupt_phase = phase
                before = self.host.engine_releases
                reports = copy.deepcopy(self.host.reports)
                code, out, err = self.run_build(label, sample=self.sample_inputs())
                self.assertEqual(code, 1)
                self.assertIn("interrupted", out + err)
                self.assertIsNone(self.host.engine_holder)
                self.assertEqual(self.host.engine_releases, before + 1)
                if phase == "cluster":
                    self.assertIn("interrupted; the clustering continues in the worker", out)
                    self.assertIsNotNone(self.host.cluster_live_id)
                    self.assertEqual(self.host.reports, reports)
                    self.assertNotIn("report:", out)
                self.assert_sample_fix(out + err, label)

    def test_sample_requires_its_worker_and_judges_lockfile_state_on_production(self) -> None:
        label = self.fixture.labels[0]
        self.host.health[WORKER_SERVICE_NAME] = "unhealthy"
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"sibling {WORKER_SERVICE_NAME}", err)
        self.assertIn("sudo python3 -m tools.cistack up", err)
        self.assertIn(stack.logs_fix(CI_ROOT, WORKER_SERVICE_NAME), err)
        self.assert_sample_fix(err, label)
        self.host.health[WORKER_SERVICE_NAME] = "healthy"
        self.assertEqual(self.run_build(label, sample=self.sample_inputs())[0], 0)
        self.host.record_rows[label]["state"] = "superseded"
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("superseded", err)
        self.assertEqual(self.host.sibling_lockfiles[label]["state"], "cut")
        self.assertIsNone(self.host.engine_holder)
        self.assert_sample_fix(err, label)

    def test_sample_service_refusals_name_the_correct_projects_logs_and_retry(self) -> None:
        label = self.fixture.labels[0]
        for service in (WORKER_SERVICE_NAME, OPENSEARCH_SERVICE_NAME, QDRANT_SERVICE_NAME, EMBED.service_name):
            with self.subTest(service=service):
                self.host.health[service] = "unhealthy"
                before = self.host.engine_releases
                code, out, err = self.run_build(label, sample=self.sample_inputs())
                self.host.health[service] = "healthy"
                self.assertEqual((code, out), (1, ""))
                directory = RENDERED if service == EMBED.service_name else CI_ROOT
                self.assertIn(stack.logs_fix(directory, service), err)
                self.assertEqual(self.host.deferred, [])
                self.assertIsNone(self.host.engine_holder)
                self.assertEqual(self.host.engine_releases, before + 1)
                self.assert_sample_fix(err, label)

    def test_sample_root_profile_and_retired_refusals_keep_the_sample_retry(self) -> None:
        label = self.fixture.labels[0]
        self.host.euid = 1
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("root privileges", err)
        self.assertEqual((self.host.engine_releases, self.host.executions), (0, []))
        self.assert_sample_fix(err, label)
        self.host.euid = 0
        original_site = self.host.site_text
        self.host.site_text = (ROOT / "config/site.example.yaml").read_text() + "\nhardware_profile: 1000x96v-256d\n"
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("is not in models.lock", err)
        self.assertIsNone(self.host.engine_holder)
        self.assert_sample_fix(err, label)
        self.host.site_text = original_site
        self.assertEqual(self.run_build(label, sample=self.sample_inputs())[0], 0)
        name, row = next(iter(self.host.generations.items()))
        retired = replace(row, state="retired", promoted_at=NOW.isoformat(), retired_at=NOW.isoformat())
        self.host.generations[name] = retired
        self.host.deferred.clear()
        writes = [sql for sql in self.host.statements if "public.generation_gold" in sql]
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("generation is retired", out)
        self.assertNotIn("generation: ok", out)
        self.assertEqual([sql for sql in self.host.statements if "public.generation_gold" in sql], writes)
        self.assertEqual(self.host.generations[name], retired)
        self.assertEqual(self.host.deferred, [])
        self.assertIsNone(self.host.engine_holder)
        self.assert_sample_fix(out, label)

    def test_two_lockfiles_print_stages_and_record_distinct_generations(self) -> None:
        for label in self.fixture.labels:
            inputs = self.inputs(label)
            code, out, err = self.run_build(label)
            self.assertEqual((code, err), (0, ""), out)
            for stage in ("preconditions", "smoke", "generation", "guard", "reserve", "cluster", "build", "counts", "dense", "built", "treatment", "report"):
                self.assertIn(f"{stage}: ok", out)
            self.assertIn(inputs.name, out)
            self.assertIn("building, created", out)
            self.assertIn("ready 3, chunked 2, chunks 11, projected chunks 17", out)
            self.assertIn("counted none = 0", out)
            self.assertIn("documents ready 3, chunked 2, unchunked 1", out)
            self.assertIn("chunks indexed 7, present 4, leveled 0, total 11", out)
            self.assertIn("bytes 1024", out)
            self.assertIn("points 11, indexed 11, written 8 (present 3, leveled 0, copied 2, embedded 6)", out)
            self.assertIn(f"bytes lexical 1024, dense {self.host.du_bytes}, total {1024 + self.host.du_bytes}", out)
            self.assertIn("state building", out)
            job = self.host.deferred[-1]
            self.assertEqual(job["snapshot"], inputs.snapshot)
            self.assertEqual(job["courts"].keys(), set(inputs.courts))
            self.assertEqual(job["inputs_hash"], inputs.inputs_hash)
            self.assertEqual(job["embedding_space"], inputs.embedding_space)
            materialized = self.host.treatment_results[-1]
            self.assertIn(f"treatment: ok — {inputs.name}: {treatment.summary(materialized)}", out)
            self.assertEqual(self.host.reports[-1]["treatment"], materialized.to_json())
            self.assertGreater(out.index("treatment: ok"), out.index("built: ok"))
            self.assertGreater(out.index("report: ok"), out.index("treatment: ok"))
            self.assertIsNone(self.host.lock_holder)
            sql = next(sql for sql in reversed(self.host.statements) if guards.CHUNK_PROJECTION_SQL in sql)
            self.assertEqual(self.host._bound(sql, "v_source"), self.fixture.source.name)
            self.assertEqual(self.host._bound(sql, "v_snapshot_date"), inputs.snapshot[-10:])
            self.assertEqual(self.host._bound(sql, "v_courts"), "{" + ",".join(inputs.courts) + "}")
            self.assertEqual(self.host._bound(sql, "v_pipeline"), inputs.pipeline)
        self.assertEqual(len(self.host.generations), 2)
        self.assertEqual(len(self.host.treatment_payloads), len(self.fixture.labels))
        self.assertEqual(self.host.lock_releases, 2)

    def seed_resident(self, state: str, *, name: str | None = None) -> GenerationRow:
        """Seed a distinct generation with record counts deliberately unlike the store."""

        inputs = self.inputs(self.fixture.labels[0])
        completed = state in {"built", "live", "retired"}
        row = replace(
            row_for(), gen_id=inputs.name if name is None else name, corpus_lockfile=inputs.label,
            lockfiles=(inputs.label,), pipeline=inputs.pipeline, embedding_space=inputs.embedding_space,
            inputs_hash=inputs.inputs_hash, state=state, counts={"dense_points": 999999999},
            dense_built_at=NOW.isoformat() if completed else None,
            built_at=NOW.isoformat() if completed else None,
            promoted_at=NOW.isoformat() if state in {"live", "retired"} else None,
            retired_at=NOW.isoformat() if state == "retired" else None,
            failure_reason="store" if state == "failed" else None,
        )
        self.host.generations[row.gen_id] = row
        return row

    def test_guard_counts_retired_built_and_unrecorded_collections_from_the_store(self) -> None:
        """The guard reads exact counts, orders names, and skips a collection gone at count."""

        retired = self.seed_resident("retired")
        built_name = generations.generation_name("caselaw", retired.corpus_lockfile, retired.embedding_space + "-other")
        built = self.seed_resident("built", name=built_name)
        self.host.qdrant_points = {retired.gen_id: 7, built.gen_id: 11, "fictitious-unrecorded": 5, "gone": None}
        code, out, err = self.run_build(self.fixture.labels[1])
        self.assertEqual((code, err), (0, ""), out)
        ordered = sorted(((retired.gen_id, "retired", 7), (built.gen_id, "built", 11), ("fictitious-unrecorded", "unrecorded", 5)))
        expected = ", ".join(f"{name} ({state}) {points}" for name, state, points in ordered)
        guard_line = next(line for line in out.splitlines() if line.startswith("guard:"))
        self.assertIn(f"counted {expected} = 23", guard_line)
        limits = build.chunk_guards(self.host, self.fixture.checkout, SITE_PATH, build.COMMAND_PATH)
        assert isinstance(limits, models.ChunkGuards)
        self.assertIn(f"23 + 17 × {limits.serving_ceiling_chunks}/{limits.build_refusal_chunks} ≤ serving ceiling {limits.serving_ceiling_chunks}", guard_line)
        self.assertNotIn("gone", guard_line)
        self.assertEqual(self.host.generations[retired.gen_id], retired)
        self.assertEqual(self.host.generations[built.gen_id], built)
        self.assertEqual(self.host.store_requests, [
            (RENDERED, "GET", "/collections", None),
            *((RENDERED, "POST", f"/collections/{name}/points/count", {"exact": True})
              for name in sorted(self.host.qdrant_points)),
        ])
        listing = next(i for i, (_, text, _) in enumerate(self.host.executions)
                       if text and text.startswith("GET /collections HTTP"))
        before_listing = [text for _, text, _ in self.host.executions[:listing] if text]
        self.assertTrue(any("ORDER BY collection, created_at, gen_id" in text for text in before_listing))
        for row in (retired, built):
            for prefix in lexical.JOB_SUBJECTS:
                self.assertTrue(any(worker.bind("v_lock", prefix + row.gen_id) in text for text in before_listing))

    def test_resumed_candidate_store_points_are_excluded_from_the_guard(self) -> None:
        label = self.fixture.labels[0]
        self.host.interrupt_phase = "dense"
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("interrupted; the embed continues in the worker", out)
        name = self.inputs(label).name
        job = self.host.dense_live_id
        assert job is not None
        self.host.interrupt_phase = None
        self.host.qdrant_points[name] = 999999999
        deferred = len(self.host.deferred)
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        guard_line = next(line for line in out.splitlines() if line.startswith("guard:"))
        self.assertIn("counted none = 0", guard_line)
        self.assertNotIn("999999999", guard_line)
        self.assertIn(f"dense: ok — {name}: rejoined job {job}", out)
        self.assertEqual(len(self.host.deferred), deferred + 2)
        self.assertIn((RENDERED, "POST", f"/collections/{name}/points/count", {"exact": True}), self.host.store_requests)

    def test_sample_guard_counts_the_sibling_store_with_its_own_key(self) -> None:
        label = self.fixture.labels[0]
        self.host.qdrant_points = {"production-only": 999999999}
        self.host.sibling_qdrant_points = {"sibling-only": 13}
        code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("counted sibling-only (unrecorded) 13 = 13", out)
        self.assertNotIn("production-only", out)
        self.assertEqual(secrets.current_directory(), Path(CI_SECRETS_DIR))
        key_path = str(Path(CI_SECRETS_DIR) / QDRANT_SECRET_NAME)
        self.assertEqual(self.host.secret_reads, [key_path])
        for argv, request, _ in self.host.executions:
            if QDRANT_SERVICE_NAME not in argv or request is None:
                continue
            self.assertIn(str(CI_ROOT), " ".join(argv))
            key = self.host.secret_files[key_path].strip()
            self.assertIn(f"api-key: {key}\r\n", request)
            self.assertNotIn(key, " ".join(argv) + out + err)
        self.assertIn((str(CI_ROOT), "GET", "/collections", None), self.host.store_requests)
        projection_directory = next(directory for directory, sql in self.host.statement_directories
                                    if guards.CHUNK_PROJECTION_SQL in sql)
        self.assertEqual(projection_directory, RENDERED)
        resident_directory = next(directory for directory, sql in self.host.statement_directories
                                  if "ORDER BY collection, created_at, gen_id" in sql)
        self.assertEqual(resident_directory, str(CI_ROOT))

    def test_skipped_sample_guard_still_prints_its_counted_collections(self) -> None:
        self.host.sibling_qdrant_points = {"fictitious-resident": 13}
        for counts, reason in (({"ready": 0, "chunked": 0, "chunks": 0}, "no ready documents"),
                               ({"ready": 101, "chunked": 1, "chunks": 7}, "fewer than 1 %")):
            with self.subTest(counts=counts):
                self.host.projection_counts = counts
                code, out, err = self.run_build(self.fixture.labels[0], sample=self.sample_inputs())
                self.assertEqual((code, err), (0, ""), out)
                line = next(line for line in out.splitlines() if line.startswith("guard:"))
                self.assertIn(f"skipped: {reason}", line)
                self.assertIn("counted fictitious-resident (unrecorded) 13 = 13", line)

    def test_store_or_resident_record_failure_refuses_before_jobs_and_reports(self) -> None:
        for phase in ("resident-record", "qdrant-listing", "qdrant-count"):
            with self.subTest(phase=phase):
                self.host.fail_phase = phase
                self.host.qdrant_points = {"fictitious-unrecorded": 7}
                code, out, err = self.run_build(self.fixture.labels[1])
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn("guard: refuse", out)
                self.assertIn("exit 3", out)
                self.assertIn("Fix:", out)
                self.assertNotIn("private diagnostic", out)
                self.assertEqual((self.host.deferred, self.host.jobs, self.host.reports), ([], {}, []))
                self.assertNotIn("cluster:", out)
                self.assertNotIn("report:", out)
                if phase != "resident-record":
                    self.assertIn(stack.logs_fix(RENDERED, QDRANT_SERVICE_NAME), out)
                self.assertIsNone(self.host.lock_holder)

    def test_interrupted_other_build_with_dense_job_refuses_before_the_store(self) -> None:
        """The guard treats a worker left running after its host lock releases as growing."""

        self.host.interrupt_phase = "dense"
        code, out, err = self.run_build(self.fixture.labels[0])
        self.assertEqual((code, err), (1, ""), out)
        self.assertIsNone(self.host.lock_holder)
        job = self.host.dense_live_id
        assert job is not None
        name = self.host.jobs[job]["name"]
        self.host.interrupt_phase = None
        self.host.store_requests.clear()
        before = copy.deepcopy((self.host.deferred, self.host.jobs, self.host.reports))
        code, out, err = self.run_build(self.fixture.labels[1])
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn(f"guard: refuse — {name}: dense job {job} is queued or running", out)
        self.assertIn(report.command(f"index build caselaw --lockfile {self.fixture.labels[0]}"), out)
        self.assertEqual((self.host.deferred, self.host.jobs, self.host.reports), before)
        self.assertEqual(self.host.store_requests, [])
        self.assertNotIn("report:", out)

    def test_other_generations_jobs_refuse_even_before_a_collection_exists(self) -> None:
        """Every recorded state and all three subjects must be idle before counting."""

        for state in ("building", "built", "live", "retired", "failed"):
            for prefix, subject in lexical.JOB_SUBJECTS.items():
                for status in ("todo", "doing"):
                    with self.subTest(state=state, prefix=prefix, status=status):
                        row = self.seed_resident(state)
                        self.host.live_jobs = {prefix + row.gen_id: (731, status)}
                        code, out, err = self.run_build(self.fixture.labels[1])
                        self.assertEqual((code, err), (1, ""), out)
                        self.assertIn(f"guard: refuse — {row.gen_id}: {subject} job 731 is queued or running", out)
                        self.assertIn(report.command(f"index build caselaw --lockfile {row.corpus_lockfile}"), out)
                        self.assertIn(report.command(f"index build caselaw --lockfile {self.fixture.labels[1]}"), out)
                        self.assertEqual(self.host.store_requests, [])
                        self.assertEqual((self.host.deferred, self.host.jobs, self.host.reports), ([], {}, []))
                        self.assertNotIn("cluster:", out)
                        self.assertNotIn("report:", out)
                        self.assertIsNone(self.host.lock_holder)

    def test_guard_and_reserve_refusals_defer_no_job_and_write_no_build_report(self) -> None:
        label = self.fixture.labels[0]
        inputs = self.inputs(label)
        loaded = models.load_models_lock(self.fixture.checkout / "models.lock")
        assert loaded.lock is not None
        profile = loaded.lock.profile(loaded.lock.reference)
        assert profile is not None
        for stage in ("guard", "reserve"):
            with self.subTest(stage=stage):
                self.host.statements.clear()
                self.host.projection_counts = {"ready": 100, "chunked": 1, "chunks": 60000} if stage == "guard" else {"ready": 3, "chunked": 2, "chunks": 11}
                self.host.promoted = (PromotedRow("fictitious-live", "caselaw", 13_000_000),)
                code, out, err = self.run_build(label)
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn(f"{stage}: refuse", out)
                self.assertEqual(self.host.deferred, [])
                self.assertEqual(self.host.jobs, {})
                self.assertEqual(self.host.reports, [])
                self.assertFalse(any("public.build_reports" in sql or "procrastinate_jobs" in sql
                                     or "procrastinate_defer_jobs_v1" in sql for sql in self.host.statements))
                self.assertNotIn("cluster:", out)
                self.assertNotIn("report:", out)
                self.assertIsNone(self.host.lock_holder)
                self.assertIn(inputs.name, self.host.generations)
                self.assertIn(report.command(f"index build caselaw --lockfile {label}"), out)
                if stage == "guard":
                    self.assertIn(f"{inputs.name}: ready 100, chunked 1, chunks 60000, projected chunks 6000000", out)
                    self.assertIn(f"build refusal {profile.chunk_guards.build_refusal_chunks}, serving ceiling {profile.chunk_guards.serving_ceiling_chunks}; counted none = 0", out)
                    self.assertIn(report.command("corpus cut"), out)
                    self.assertIn("hardware_profile in /etc/gideon/site.yaml", out)
                    self.assertIn("guards derive from the profile's RAM", out)
                    self.assertIn("docs/runbooks/release-files.md §4", out)
                    self.assertNotIn("reserve:", out)
                else:
                    verdict = reserve.judge(self.host.free_bytes, self.host.promoted[0].bytes)
                    self.assertIn(reserve.describe(verdict), out)
                    self.assertIn(reserve.RUNBOOK_ROW, out)
                    self.assertIn("Free space under /data", out)

    def test_guard_skips_no_ready_documents_and_less_than_one_percent_chunked(self) -> None:
        for counts, reason in (
            ({"ready": 0, "chunked": 0, "chunks": 0}, "no ready documents"),
            ({"ready": 101, "chunked": 1, "chunks": 60000}, "fewer than 1 % of ready documents chunked"),
        ):
            with self.subTest(counts=counts):
                self.host.projection_counts = counts
                code, out, err = self.run_build()
                self.assertEqual((code, err), (0, ""), out)
                self.assertIn(f"guard: ok — skipped: {reason};", out)
                self.assertIn("counted none = 0", out)
                self.assertIn(f"ready {counts['ready']}, chunked {counts['chunked']}, chunks {counts['chunks']}", out)
                self.assertIn("cluster: ok", out)

    def test_reserve_sums_live_rows_for_the_collection_and_prints_both_lines(self) -> None:
        self.host.promoted = (
            PromotedRow("fictitious-live-a", "caselaw", 5_000_000),
            PromotedRow("fictitious-live-b", "caselaw", 7_000_000),
            PromotedRow("fictitious-other", "authorities", self.host.free_bytes * 100),
        )
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"reserve: ok — {reserve.describe(reserve.judge(self.host.free_bytes, 12_000_000))}", out)
        self.assertIn("build line 15000000 bytes (0.0 GB), cleared", out)
        self.assertIn("alert line 24000000 bytes (0.0 GB), not cleared", out)

    def test_reserve_skips_without_a_live_row_for_the_collection_and_reads_free_bytes(self) -> None:
        for rows in ((), (PromotedRow("fictitious-other", "authorities", 99_000_000),)):
            with self.subTest(rows=rows):
                self.host.promoted = rows
                self.host.calls.clear()
                code, out, err = self.run_build()
                self.assertEqual((code, err), (0, ""), out)
                self.assertIn("reserve: ok — skipped: no promoted generation for caselaw; reserve not judged; "
                              f"/data free {self.host.free_bytes} bytes", out)
                self.assertIn(list(reserve.DATA_DF_ARGV), self.host.calls)

    def test_guard_and_reserve_read_failures_stop_before_jobs_or_reports(self) -> None:
        for phase in ("guard", "reserve", "df"):
            with self.subTest(phase=phase):
                self.host.fail_phase = phase
                code, out, err = self.run_build()
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn(f"{'guard' if phase == 'guard' else 'reserve'}: refuse", out)
                self.assertIn("Fix:", out)
                self.assertIn(report.command("index build caselaw"), out)
                self.assertNotIn("private diagnostic", out)
                self.assertEqual(self.host.deferred, [])
                self.assertEqual(self.host.reports, [])
                self.assertIsNone(self.host.lock_holder)
        self.host.fail_phase = None
        self.host.df_text = "invalid fictitious df output"
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("reserve: refuse", out)
        self.assertIn("Ensure /data is mounted and readable", out)
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.reports, [])

    def test_built_measures_the_vector_directory_and_records_both_byte_halves(self) -> None:
        label = self.fixture.labels[0]
        name = self.inputs(label).name
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        directory = f"{QDRANT_DATA_ROOT}/collections/{name}"
        self.assertIn(["du", "-sb", directory], self.host.calls)
        self.assertEqual(self.host.footprints, [{"lexical_bytes": 1024, "dense_bytes": self.host.du_bytes,
                                                "bytes": 1024 + self.host.du_bytes}])
        row = self.host.generations[name]
        self.assertEqual(row.bytes, row.counts["lexical_bytes"] + row.counts["dense_bytes"])
        self.assertIn(f"bytes lexical {row.counts['lexical_bytes']}, dense {row.counts['dense_bytes']}, total {row.bytes}", out)
        sql = next(sql for sql in self.host.statements if ":'v_dense_bytes'" in sql)
        self.assertEqual(self.host._bound(sql, "v_name"), name)
        self.assertEqual(self.host._bound(sql, "v_dense_bytes"), str(self.host.du_bytes))
        self.assertEqual(self.host.reports[-1]["state"], "built")

    def test_du_failures_refuse_built_without_recording_a_footprint(self) -> None:
        label = self.fixture.labels[0]
        name = self.inputs(label).name
        directory = f"{QDRANT_DATA_ROOT}/collections/{name}"
        for phase, text, error in (
            ("du", None, None),
            (None, None, FileNotFoundError()),
            (None, None, subprocess.SubprocessError()),
            (None, "invalid", None),
            (None, f"-1\t{directory}\n", None),
            (None, "2048\t/fictitious-wrong-directory\n", None),
        ):
            with self.subTest(phase=phase, text=text, error=error):
                self.host.fail_phase, self.host.du_text, self.host.du_error = phase, text, error
                code, out, err = self.run_build(label)
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn("built: refuse", out)
                self.assertIn(directory, out)
                self.assertIn(stack.logs_fix(RENDERED, QDRANT_SERVICE_NAME), out)
                self.assertIn(report.command(f"index build caselaw --lockfile {label}"), out)
                self.assertNotIn("private diagnostic", out)
                self.assertEqual(self.host.footprints, [])
                self.assertNotIn("dense_bytes", self.host.generations[name].counts)
                self.assertEqual(self.host.reports[-1]["failed_stage"], "built")
                self.assertEqual(self.host.treatment_payloads, [])
                self.assertIsNone(self.host.lock_holder)
        self.host.fail_phase, self.host.du_text, self.host.du_error = None, None, None
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("bytes lexical", out)
        self.assertEqual(len(self.host.footprints), 1)

    def test_footprint_write_failure_refuses_built_and_names_the_database_fix(self) -> None:
        self.host.fail_phase = "footprint"
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("built: refuse", out)
        self.assertIn("generation footprint write failed", out)
        self.assertIn(stack.logs_fix(RENDERED, "postgres"), out)
        self.assertIn(report.command("index build caselaw"), out)
        self.assertEqual(self.host.footprints, [])
        self.assertEqual(self.host.reports[-1]["failed_stage"], "built")

    def test_interrupts_during_guards_or_built_preserve_the_row_and_write_no_report(self) -> None:
        for phase in ("guard", "reserve", "du", "footprint"):
            with self.subTest(phase=phase):
                self.host.interrupt_phase = phase
                self.host.deferred.clear()
                code, out, err = self.run_build()
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn("interrupted; the row stands", out)
                if phase in {"du", "footprint"}:
                    self.assertIn("built: refuse", out)
                    self.assertIn("the footprint is not recorded", out)
                else:
                    self.assertIn(f"{phase}: refuse", out)
                    self.assertEqual(self.host.deferred, [])
                self.assertIn(report.command("index build caselaw"), out)
                self.assertEqual(self.host.reports, [])
                self.assertEqual(self.host.footprints, [])
                self.assertIsNone(self.host.lock_holder)

    def test_absent_treatment_pin_materializes_with_null_snapshot_bindings(self) -> None:
        label = self.fixture.labels[0]
        inputs = self.inputs(label)
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(self.host.treatment_payloads), 1)
        payload = self.host.treatment_payloads[0]
        self.assertEqual(payload["gen_id"], inputs.name)
        read_payload = self.host.treatment_reads[0]
        self.assertEqual(read_payload["source"], inputs.snapshot[:-staging.DATE_SUFFIX_LENGTH])
        self.assertEqual(read_payload["snapshot_date"], inputs.snapshot[-10:])
        self.assertEqual(read_payload["courts"], list(inputs.courts))
        self.assertIsNone(payload["treatment_source"])
        self.assertIsNone(payload["treatment_snapshot_date"])
        self.assertIsNone(self.host.reports[0]["treatment"]["treatment_snapshot"])

    def test_selected_lockfile_supplies_treatment_snapshot_without_changing_the_preconditions_row(self) -> None:
        preconditions = {}
        for label in self.fixture.labels:
            code, out, err = self.run_build(label)
            self.assertEqual((code, err), (0, ""), out)
            preconditions[label] = next(line for line in out.splitlines() if line.startswith("preconditions:"))
        captured, _, _ = self.fixture._add_captured_source()
        registry = (self.fixture.source, captured)
        known_sources = {source.name: source for source in registry}
        for index, label in enumerate(self.fixture.labels):
            path = self.fixture.checkout / f"corpus/lockfiles/{label}.yaml"
            loaded = load_lockfile(path, known_sources=known_sources)
            assert loaded.lockfile is not None
            lockfile = loaded.lockfile
            pin = lockfile.sources[OVERRULED_SOURCE]
            pin = replace(pin, snapshot_date=(date.fromisoformat(pin.snapshot_date) + timedelta(days=index)).isoformat())
            path.write_text(render_lockfile(replace(lockfile, sources={**lockfile.sources, OVERRULED_SOURCE: pin})))
            self.host.record_rows[label]["sources"].append({
                "source": OVERRULED_SOURCE, "snapshot_date": pin.snapshot_date,
            })
        for label in self.fixture.labels:
            with self.subTest(label=label):
                loaded = load_lockfile(self.fixture.checkout / f"corpus/lockfiles/{label}.yaml", known_sources=known_sources)
                assert loaded.lockfile is not None
                pin = loaded.lockfile.sources[OVERRULED_SOURCE]
                code, out, err = self.run_build(label, sources=registry)
                self.assertEqual((code, err), (0, ""), out)
                self.assertIn(preconditions[label], out.splitlines())
                payload = self.host.treatment_payloads[-1]
                self.assertEqual(payload["treatment_source"], OVERRULED_SOURCE)
                self.assertEqual(payload["treatment_snapshot_date"], pin.snapshot_date)
                self.assertEqual(self.host.reports[-1]["treatment"]["treatment_snapshot"],
                                 f"{OVERRULED_SOURCE}-{pin.snapshot_date}")

    def test_newest_default(self) -> None:
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.host.deferred[0]["label"], self.fixture.labels[-1])

    def test_same_state_under_another_label_finds_the_original_generation(self) -> None:
        first, second = self.fixture.labels
        self.assertEqual(self.run_build(first)[0], 0)
        directory = self.fixture.checkout / "corpus/lockfiles"
        loaded = load_lockfile(directory / f"{first}.yaml", known_sources={"example": replace(SOURCES[0], name="example")})
        assert loaded.lockfile is not None
        shutil.copytree(directory / first, directory / second, dirs_exist_ok=True)
        (directory / f"{second}.yaml").write_text(render_lockfile(replace(loaded.lockfile, label=second)))
        shutil.rmtree(staging.work_directory(second, self.fixture.source.name, work_root=self.fixture.work))
        self.fixture.seed_report_work(second)
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
            reports = len(self.host.reports)
            materializations = len(self.host.treatment_payloads)
            code, out, err = self.run_build(label)
            self.assertEqual(err, "")
            self.assertEqual(code, 0 if state == "live" else 1)
            self.assertIn("generation is live; nothing built" if state == "live" else "generation is retired", out)
            self.assertNotIn("counts:", out)
            self.assertEqual(self.host.deferred, [])
            self.assertEqual(len(self.host.reports), reports)
            self.assertEqual(len(self.host.treatment_payloads), materializations)
            self.assertNotIn("treatment:", out)
            self.assertNotIn("report:", out)
            self.assertIsNone(self.host.lock_holder)

    def test_rejoins_a_live_job_without_deferring_again(self) -> None:
        label = self.fixture.labels[0]
        self.assertEqual(self.run_build(label)[0], 0)
        job = next(job for job, task in self.host.task_by_job.items() if task == LEXICAL_TASK)
        self.host.live_id = job
        count = len(self.host.deferred)
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"rejoined job {job}", out)
        self.assertEqual(len(self.host.deferred), count + 2)

    def test_minute_heartbeat_reads_recorded_counts_without_real_waiting(self) -> None:
        self.host.pending_polls = 121
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        heartbeat = "building, documents 3, chunks indexed 7, seconds 5"
        self.assertEqual(out.count(heartbeat), 2)
        self.assertEqual(self.host.generation_reads, 7)
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
        for phase in ("generation", "cluster-live-job", "cluster-defer", "cluster", "cluster-counts", "live-job", "defer", "build", "counts",
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
        for phase, text in (("preconditions", "nothing was recorded"), ("smoke", "nothing was recorded"),
                            ("generation", "nothing was recorded"),
                            ("build", "the build continues in the worker"), ("counts", "the row stands")):
            with self.subTest(phase=phase):
                self.host.interrupt_phase = phase
                reports = len(self.host.reports)
                code, out, err = self.run_build()
                self.assertEqual(code, 1)
                self.assertIn(text, out + err)
                self.assertIn("Fix:", out + err)
                self.assertIsNone(self.host.lock_holder)
                self.assertEqual(len(self.host.reports), reports)
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

    def _superseded_building(self, label: str) -> GenerationRow:
        """Leave a former live generation in the state restored short stores require."""

        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        built = self.host.generations[self.inputs(label).name]
        self.host.generations[built.gen_id] = replace(built, state="live", promoted_at=NOW.isoformat())
        self.fixture._hold_record("installed", self.fixture.labels[-1])
        self.host.record_rows[label]["state"] = "superseded"
        demoted = replace(built, state="building", dense_built_at=None, built_at=None, promoted_at=None)
        self.host.generations[built.gen_id] = demoted
        self.host.statements.clear()
        return demoted

    def test_superseded_label_resumes_its_failed_generation(self) -> None:
        label = self.fixture.labels[0]
        building = self._superseded_building(label)
        row = replace(building, state="failed", failure_reason="store")
        self.host.generations[row.gen_id] = row
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"lockfile {label} is superseded; resuming {row.gen_id}", out.splitlines()[0])
        self.assertIn(f"generation: ok — {row.gen_id}: failed, found", out)
        self.assertEqual(tuple(self.host.generations), (row.gen_id,))
        self.assertEqual(self.host.generations[row.gen_id].state, "built")

    def test_superseded_label_resumes_its_building_generation_under_current_inputs(self) -> None:
        label = self.fixture.labels[0]
        row = self._superseded_building(label)
        order = Mock()
        with (
            patch.object(build, "embed_target", wraps=build.embed_target) as embed,
            patch.object(build, "collection_inputs", wraps=build.collection_inputs) as inputs,
            patch.object(build.indexrecord, "read_generation_by_inputs", wraps=build.indexrecord.read_generation_by_inputs) as read,
        ):
            order.attach_mock(embed, "embed_target")
            order.attach_mock(inputs, "collection_inputs")
            order.attach_mock(read, "read_generation_by_inputs")
            code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual([call[0] for call in order.mock_calls], ["embed_target", "collection_inputs", "read_generation_by_inputs"])
        read.assert_called_once_with(
            self.host, RENDERED, row.collection, row.inputs_hash,
            command_path=f"index build {row.collection} --lockfile {label}",
        )
        self.assertIn(f"lockfile {label} is superseded; resuming {row.gen_id}", out.splitlines()[0])
        self.assertIn(f"generation: ok — {row.gen_id}: building, found", out)
        self.assertEqual(tuple(self.host.generations), (row.gen_id,))
        self.assertEqual(self.host.generations[row.gen_id].state, "built")
        self.assertEqual(self.host.generations[row.gen_id].clustered_at, row.clustered_at)
        read_sql = next(i for i, sql in enumerate(self.host.statements) if "WHERE collection = :'v_collection'" in sql)
        write_sql = next(i for i, sql in enumerate(self.host.statements) if "DO $generation$" in sql)
        self.assertLess(read_sql, write_sql)
        self.assertIn(worker.bind("v_inputs_hash", row.inputs_hash), self.host.statements[read_sql])

    def test_superseded_label_without_a_generation_checks_inputs_then_refuses_without_writes(self) -> None:
        label = self.fixture.labels[0]
        self.host.record_rows[label]["state"] = "superseded"
        order = Mock()
        with (
            patch.object(build, "embed_target", wraps=build.embed_target) as embed,
            patch.object(build, "collection_inputs", wraps=build.collection_inputs) as inputs,
            patch.object(build.indexrecord, "read_generation_by_inputs", wraps=build.indexrecord.read_generation_by_inputs) as read,
        ):
            order.attach_mock(embed, "embed_target")
            order.attach_mock(inputs, "collection_inputs")
            order.attach_mock(read, "read_generation_by_inputs")
            code, out, err = self.run_build(label)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual([call[0] for call in order.mock_calls], ["embed_target", "collection_inputs", "read_generation_by_inputs"])
        self.assertIn(f"lockfile {label} is superseded", err)
        self.assertIn(f"Use a current lockfile with {report.command('index build caselaw')}, then retry.", err)
        self.assertEqual((self.host.generations, self.host.deferred, self.host.reports), ({}, [], []))
        self.assertFalse(any("DO $generation$" in sql for sql in self.host.statements))

    def test_superseded_label_refuses_built_live_and_retired_generations_without_writes(self) -> None:
        label = self.fixture.labels[0]
        building = self._superseded_building(label)
        for state in ("built", "live", "retired"):
            with self.subTest(state=state):
                row = replace(
                    building, state=state, dense_built_at=building.lexical_built_at,
                    built_at=building.lexical_built_at,
                    promoted_at=NOW.isoformat() if state in ("live", "retired") else None,
                    retired_at=NOW.isoformat() if state == "retired" else None,
                )
                self.host.generations[row.gen_id] = row
                self.host.statements.clear()
                before = copy.deepcopy((self.host.generations, self.host.record_rows, self.host.jobs, self.host.deferred, self.host.reports))
                code, out, err = self.run_build(label)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"lockfile {label} is superseded", err)
                self.assertIn(f"Use a current lockfile with {report.command('index build caselaw')}, then retry.", err)
                self.assertEqual((self.host.generations, self.host.record_rows, self.host.jobs, self.host.deferred, self.host.reports), before)
                self.assertTrue(any("WHERE collection = :'v_collection'" in sql for sql in self.host.statements))
                self.assertFalse(any("DO $generation$" in sql for sql in self.host.statements))

    def test_superseded_label_refuses_a_building_row_that_does_not_name_it(self) -> None:
        label = self.fixture.labels[0]
        row = self._superseded_building(label)
        other = self.fixture.labels[-1]
        foreign = replace(
            row, gen_id=generations.generation_name(row.collection, other, row.embedding_space),
            corpus_lockfile=other, lockfiles=(other,),
        )
        self.host.generations = {foreign.gen_id: foreign}
        before = copy.deepcopy((self.host.generations, self.host.deferred, self.host.reports))
        code, out, err = self.run_build(label)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"lockfile {label} is superseded", err)
        self.assertIn("Use a current lockfile", err)
        self.assertEqual((self.host.generations, self.host.deferred, self.host.reports), before)
        self.assertFalse(any("DO $generation$" in sql for sql in self.host.statements))

    def test_superseded_label_with_a_changed_embedding_profile_refuses_a_new_generation(self) -> None:
        label = self.fixture.labels[0]
        row = self._superseded_building(label)
        path = self.fixture.checkout / "models.lock"
        data = yaml.safe_load(path.read_text())
        alternative = "1000x96v-256d"
        data["profiles"][alternative] = copy.deepcopy(data["profiles"][data["reference"]])
        data["profiles"][alternative]["embedding_space"]["id"] = "fictitious-resume-space"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.host.site_text = (ROOT / "config/site.example.yaml").read_text() + f"\nhardware_profile: {alternative}\n"
        loaded_models = models.load_models_lock(path)
        assert loaded_models.lock is not None
        profile = models.select_profile(loaded_models.lock, alternative)
        assert isinstance(profile, models.HardwareProfile) and profile.embedding_space is not None
        loaded_lockfile = load_lockfile(
            self.fixture.checkout / f"corpus/lockfiles/{label}.yaml",
            known_sources={self.fixture.source.name: self.fixture.source},
        )
        assert loaded_lockfile.lockfile is not None
        expected_hash = generations.inputs_hash(
            loaded_lockfile.lockfile, row.collection, profile.embedding_space.id, sources=(self.fixture.source,),
        )
        self.assertNotEqual(expected_hash, row.inputs_hash)
        before = copy.deepcopy((self.host.generations, self.host.deferred, self.host.reports))

        code, out, err = self.run_build(label)

        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"lockfile {label} is superseded", err)
        self.assertIn("Use a current lockfile", err)
        self.assertEqual((self.host.generations, self.host.deferred, self.host.reports), before)
        read_sql = next(sql for sql in self.host.statements if "WHERE collection = :'v_collection'" in sql)
        self.assertIn(worker.bind("v_inputs_hash", expected_hash), read_sql)
        self.assertFalse(any("DO $generation$" in sql for sql in self.host.statements))

    def test_superseded_sample_refuses_before_reading_any_build_inputs(self) -> None:
        label = self.fixture.labels[0]
        row = self._superseded_building(label)
        with (
            patch.object(build, "embed_target", wraps=build.embed_target) as embed,
            patch.object(build, "collection_inputs", wraps=build.collection_inputs) as inputs,
            patch.object(build.indexrecord, "read_generation_by_inputs", wraps=build.indexrecord.read_generation_by_inputs) as read,
        ):
            code, out, err = self.run_build(label, sample=self.sample_inputs())
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"lockfile {label} is superseded", err)
        self.assertIn(f"Use a current lockfile with {report.command(f'index build caselaw --sample --lockfile {label}')}, then retry.", err)
        embed.assert_not_called()
        inputs.assert_not_called()
        read.assert_not_called()
        self.assertEqual(self.host.generations[row.gen_id], row)
        self.assertFalse(any("DO $generation$" in sql for sql in self.host.statements))

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
        data = yaml.safe_load((self.fixture.checkout / "models.lock").read_text())
        reference = data["reference"]
        alternative = "1000x96v-256d"
        data["profiles"][alternative] = copy.deepcopy(data["profiles"][reference])
        data["profiles"][alternative]["embedding_space"]["id"] = "another-example-space"
        data["profiles"][alternative]["embedding_space"]["dimensions"] = 19
        data["profiles"][alternative]["models"]["embed"]["serve"]["served_name"] = "other-example-embed"
        data["profiles"][alternative]["chunk_guards"].update(serving_ceiling_million_chunks=4, build_refusal_million_chunks=2)
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
        self.assertIn(f"build refusal {selected.chunk_guards.build_refusal_chunks}, serving ceiling {selected.chunk_guards.serving_ceiling_chunks}; counted none = 0", out)
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

    def test_cluster_deferred_report_reads_every_recorded_figure(self) -> None:
        label = self.fixture.labels[0]
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        name = self.inputs(label).name
        job = next(job for job, task in self.host.task_by_job.items() if task == DUPES_TASK)
        self.assertIn(f"cluster: ok — {name}: deferred job {job}", out)
        self.assertIn(
            f"{name}: clusters 3 over 59 chunks of 60 (31 near-duplicate texts of 35), "
            f"boilerplate 2 clusters over 56 chunks (93.3 % of 60); 9 s of the "
            f"{CLUSTER_BUDGET_SECONDS} s budget; clustered_at {NOW.isoformat()}", out,
        )
        stages = [line.split(":", 1)[0] for line in out.splitlines()]
        self.assertLess(stages.index("generation"), stages.index("guard"))
        self.assertLess(stages.index("guard"), stages.index("reserve"))
        self.assertLess(stages.index("reserve"), stages.index("cluster"))
        self.assertLess(stages.index("cluster"), stages.index("build"))
        locks = [self.host._bound(sql, "v_lock") for sql in self.host.statements if "SELECT id FROM procrastinate_jobs" in sql]
        self.assertEqual(locks[0], f"{DUPES_LOCK_PREFIX}{name}")

    def test_cluster_rejoins_its_own_live_lock(self) -> None:
        label = self.fixture.labels[0]
        self.assertEqual(self.run_build(label)[0], 0)
        job = next(job for job, task in self.host.task_by_job.items() if task == DUPES_TASK)
        self.host.cluster_live_id = job
        count = len(self.host.deferred)
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"cluster: ok — {self.host.jobs[job]['name']}: rejoined job {job}", out)
        self.assertEqual(len(self.host.deferred), count + 2)

    def test_cluster_heartbeat_uses_recorded_counts_and_the_budget(self) -> None:
        self.host.cluster_pending_polls = 121
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.count(f"clustering, documents 60, texts 35, seconds 9 of {CLUSTER_BUDGET_SECONDS}"), 2)
        self.assertEqual(self.time, 121)

    def test_cluster_reused_reports_scope_without_new_cluster_figures(self) -> None:
        self.host.cluster_counts["cluster_reused"] = 1
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"clusters stand, 60 chunks, 35 texts; clustered_at {NOW.isoformat()}", out)
        self.assertNotIn("near-duplicate texts", out)

    def test_empty_cluster_scope_reports_zero_percent(self) -> None:
        self.host.cluster_counts = dict.fromkeys(self.host.cluster_counts, 0)
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("clusters 0 over 0 chunks of 0 (0 near-duplicate texts of 0)", out)
        self.assertIn("boilerplate 0 clusters over 0 chunks (0.0 % of 0)", out)

    def test_cluster_success_without_timestamp_refuses_before_build(self) -> None:
        self.host.cluster_finish_stamp = None
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("cluster: refuse", out)
        self.assertIn("succeeded clustering job has no recorded clustered_at", out)
        self.assertIn(stack.logs_fix(RENDERED, WORKER_SERVICE_NAME), out)
        self.assertNotIn("build: ok", out)
        self.assertEqual(set(self.host.task_by_job.values()), {DUPES_TASK})
        self.assertIn("report: ok", out)
        self.assertEqual(self.host.reports[-1]["failed_stage"], "cluster")
        self.assertFalse(self.host.reports[-1]["gates"]["cluster"]["pass"])

    def test_cluster_failure_uses_fix_and_interrupt_preserves_worker_job(self) -> None:
        self.host.cluster_failure_reason = "cluster-budget"
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("cluster: refuse", out)
        self.assertIn("clustering ran past its hour budget", out)
        self.assertIn(report.command("corpus cut"), out)
        self.assertIn("incremental clustering", out)
        self.assertNotIn("build: ok", out)
        self.assertEqual(len(self.host.reports), 1)
        self.assertEqual((self.host.reports[-1]["failed_stage"], self.host.reports[-1]["failure_reason"]),
                         ("cluster", self.host.cluster_failure_reason))
        self.assertIn("report: ok", out)
        self.host.cluster_failure_reason = None
        self.host.interrupt_phase = "cluster"
        reports = copy.deepcopy(self.host.reports)
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("interrupted; the clustering continues in the worker", out)
        self.assertIn(report.command("index build caselaw"), out)
        self.assertIn("rejoin", out)
        self.assertIsNotNone(self.host.cluster_live_id)
        self.assertIsNone(self.host.lock_holder)
        self.assertEqual(self.host.reports, reports)
        self.assertNotIn("report:", out)

    def test_leveled_counts_are_reported_for_both_stores(self) -> None:
        self.host.counts["leveled"] = 2
        self.host.dense_counts["dense_leveled"] = 1
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("chunks indexed 7, present 4, leveled 2, total 11", out)
        self.assertIn("written 8 (present 3, leveled 1, copied 2, embedded 6)", out)

    def test_dense_limit_reaches_defer_and_rejoin_keeps_the_jobs_limit(self) -> None:
        label = self.fixture.labels[0]
        code, out, err = self.run_build(label, dense_limit=2)
        self.assertEqual((code, err), (0, ""), out)
        job = next(job for job, task in self.host.task_by_job.items() if task == DENSE_TASK)
        self.assertEqual(self.host.jobs[job]["limit"], 2)
        line = next(line for line in out.splitlines() if line.startswith("dense: ok") and "deferred" in line)
        self.assertIn("limit 2", line)
        self.host.dense_live_id = job
        count = len(self.host.deferred)
        code, out, err = self.run_build(label, dense_limit=5)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(self.host.deferred), count + 2)
        self.assertEqual(self.host.jobs[job]["limit"], 2)
        self.assertIn(f"rejoined job {job}", out)
        self.assertNotIn("limit 5", out)

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
        self.assertEqual(len(self.host.deferred), count + 2)
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
        self.assertFalse(any(command[:2] == ["du", "-sb"] for command in self.host.calls))
        self.assertEqual(self.host.footprints, [])
        self.assertNotIn("dense_bytes", row.counts)

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

    def test_success_writes_every_distribution_and_prints_counts_alone(self) -> None:
        label = self.fixture.labels[0]
        self.host.caselaw_counts = {
            "ca6": {
                "opinions": 3, "by_status": {"ready": 2, "failed": 1},
                "by_text_source": {"xml_harvard": 1, "plain_text": 1},
                "by_precedential": {"published": 1, "unpublished": 1, "unknown": 1},
                "by_failure_reason": {"no-text": 1},
            },
            "scotus": {
                "opinions": 2, "by_status": {"ready": 2},
                "by_text_source": {"plain_text": 2}, "by_precedential": {"unknown": 2},
                "by_failure_reason": {},
            },
        }
        self.host.caselaw_anchor_counts = {
            "ca6": {"ready": 2, "anchored": 2, "by_text_source": {
                "xml_harvard": {"documents": 1, "with_anchors": 1, "chars": 40, "anchored_chars": 20},
                "plain_text": {"documents": 1, "with_anchors": 0, "chars": 30, "anchored_chars": 0},
            }},
            "scotus": {"ready": 2, "anchored": 1, "by_text_source": {
                "plain_text": {"documents": 2, "with_anchors": 0, "chars": 70, "anchored_chars": 0},
            }},
        }
        self.host.pending_polls = 2
        self.host.dense_pending_polls = 3
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(self.host.reports), 1)
        payload = self.host.reports[0]
        inputs = self.inputs(label)
        self.assertEqual(payload["gen_id"], inputs.name)
        self.assertEqual(payload["state"], "built")
        self.assertIsNone(payload["failed_stage"])
        self.assertIsNone(payload["failure_reason"])
        self.assertEqual(payload["seconds"], self.time)
        self.assertEqual(payload["metadata_changes"], {})
        directory = staging.work_directory(label, self.fixture.source.name, work_root=self.fixture.work)
        for court in inputs.courts:
            entry = payload["courts"][court]
            self.assertEqual({key: entry[key] for key in self.host.caselaw_counts[court]}, self.host.caselaw_counts[court])
            self.assertEqual(entry["anchors"], self.host.caselaw_anchor_counts[court])
            source_figure = json.loads((directory / f"{court}.{AGREEMENT_RECORD_NAME}").read_text())
            self.assertEqual(entry["agreement"], {key: value for key, value in source_figure.items()
                                                  if key not in {"schema", "label", "snapshot", "court"}})
        self.assertEqual(payload["gates"]["accounted"]["ca6"], {"staged": 1, "recorded": 3, "ready": 2, "failed": 1, "pass": True})
        self.assertEqual(payload["gates"]["cluster"], {
            "seconds": self.host.cluster_counts["cluster_seconds"], "budget": CLUSTER_BUDGET_SECONDS,
            "reused": False, "pass": True,
        })
        self.assertEqual(payload["wall_clock"]["run_seconds"], self.time)
        self.assertEqual(payload["wall_clock"]["lexical_seconds"], self.host.counts["seconds"])
        self.assertEqual(payload["wall_clock"]["dense_seconds"], self.host.dense_counts["dense_seconds"])
        self.assertTrue(payload["wall_clock"]["whole_build"])
        self.assertEqual(len(self.host.treatment_payloads), 1)
        materialized = self.host.treatment_results[0]
        self.assertEqual(payload["treatment"], materialized.to_json())
        self.assertIn(f"treatment: ok — {inputs.name}: {treatment.summary(materialized)}", out)
        states = ", ".join(f"{state} {materialized.by_state[state]}" for state in treatment.RISK_STATES)
        line = next(line for line in out.splitlines() if line.startswith("report: ok"))
        self.assertEqual(line, f"report: ok — {inputs.name}: written_at {payload['written_at']}; courts 2; "
                         "opinions 5, ready 4, failed 1; anchored 3 of 4; agreement figures 2 of 2 courts; "
                         f"accounted 2 of 2 staged; cluster {self.host.cluster_counts['cluster_seconds']} "
                         f"of {CLUSTER_BUDGET_SECONDS} s, pass; seconds {int(self.time)}; treatment {states}")
        self.assertNotIn(str(self.fixture.work), line)
        self.assertNotIn("=", line)
        self.assertGreater(out.index("report: ok"), out.index("treatment: ok"))

    def test_rerun_appends_and_newest_reader_returns_the_found_run(self) -> None:
        label = self.fixture.labels[0]
        self.assertEqual(self.run_build(label)[0], 0)
        original = copy.deepcopy(self.host.reports[0])
        self.assertTrue(original["wall_clock"]["whole_build"])
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("built, found", out)
        self.assertEqual(len(self.host.reports), 2)
        self.assertEqual(self.host.reports[0], original)
        newest = self.host.reports[-1]
        self.assertFalse(newest["wall_clock"]["whole_build"])
        self.assertGreater(newest["written_at"], original["written_at"])
        observed = buildreport.read_report(self.host, RENDERED, self.inputs(label).name)
        self.assertIsInstance(observed, buildreport.BuildReport)
        assert isinstance(observed, buildreport.BuildReport)
        self.assertEqual(observed.to_json(), newest)

    def test_created_generation_with_a_rejoined_job_does_not_measure_whole_build(self) -> None:
        label = self.fixture.labels[0]
        inputs = self.inputs(label)
        for task in (LEXICAL_TASK, DENSE_TASK):
            with self.subTest(task=task):
                self.host.generations.clear()
                self.host.reports.clear()
                self.host.next_job += 1
                job = self.host.next_job
                self.host.jobs[job] = {"name": inputs.name}
                self.host.task_by_job[job] = task
                if task == LEXICAL_TASK:
                    self.host.live_id = job
                else:
                    self.host.dense_live_id = job
                code, out, err = self.run_build(label)
                self.assertEqual((code, err), (0, ""), out)
                self.assertIn("building, created", out)
                self.assertIn(f"rejoined job {job}", out)
                self.assertFalse(self.host.reports[-1]["wall_clock"]["whole_build"])

    def test_limited_dense_finish_writes_a_report_without_whole_build(self) -> None:
        self.host.dense_finish_stamp = None
        self.host.dense_counts["dense_limit"] = 2
        code, out, err = self.run_build()
        self.assertEqual((code, err), (0, ""), out)
        payload = self.host.reports[-1]
        self.assertFalse(payload["wall_clock"]["whole_build"])
        self.assertIsNone(payload["wall_clock"]["dense_built_at"])
        self.assertIsNone(payload["wall_clock"]["lifecycle_seconds"])
        self.assertIsNone(payload["failed_stage"])
        self.assertEqual(len(self.host.treatment_payloads), 1)
        self.assertEqual(payload["treatment"], self.host.treatment_results[0].to_json())
        self.assertIn("treatment: ok", out)
        self.assertGreater(out.index("treatment: ok"), out.index("built: ok"))

    def test_job_failures_write_the_stage_reason_and_keep_exit_one(self) -> None:
        for stage, reason in (("cluster", "cluster-budget"), ("build", "store"), ("dense", "embed")):
            with self.subTest(stage=stage):
                self.host.generations.clear()
                self.host.reports.clear()
                self.host.failure_reason = reason if stage == "build" else None
                self.host.dense_failure_reason = reason if stage == "dense" else None
                self.host.cluster_failure_reason = reason if stage == "cluster" else None
                code, out, err = self.run_build()
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn(f"{stage}: refuse", out)
                self.assertIn("report: ok", out)
                self.assertEqual(len(self.host.reports), 1)
                payload = self.host.reports[0]
                self.assertEqual((payload["failed_stage"], payload["failure_reason"], payload["state"]), (stage, reason, "failed"))
                self.assertFalse(payload["wall_clock"]["whole_build"])
                self.assertIsNone(payload["treatment"])
                self.assertEqual(self.host.treatment_payloads, [])
                self.assertNotIn("treatment:", out)

    def test_counts_and_built_refusals_write_without_a_generation_failure_reason(self) -> None:
        for stage in ("counts", "built"):
            with self.subTest(stage=stage):
                self.host.generations.clear()
                self.host.reports.clear()
                self.host.finish_stamp = None if stage == "counts" else NOW.isoformat()
                self.host.dense_finish_stamp = None
                code, out, err = self.run_build()
                self.assertEqual((code, err), (1, ""), out)
                self.assertIn(f"{stage}: refuse", out)
                self.assertIn("report: ok", out)
                payload = self.host.reports[-1]
                self.assertEqual(payload["failed_stage"], stage)
                self.assertIsNone(payload["failure_reason"])
                self.assertFalse(payload["wall_clock"]["whole_build"])
                self.assertIsNone(payload["treatment"])
                self.assertEqual(self.host.treatment_payloads, [])
                self.assertNotIn("treatment:", out)

    def test_failed_materialization_writes_the_treatment_stage_with_null_and_exits_one(self) -> None:
        label = self.fixture.labels[0]
        self.host.fail_phase = "treatment"
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("built: ok", out)
        self.assertIn("treatment: refuse", out)
        self.assertIn("treatment materialization failed (exit 3)", out)
        self.assertIn(stack.logs_fix(RENDERED, "postgres"), out)
        self.assertIn(report.command(f"index build caselaw --lockfile {label}"), out)
        self.assertNotIn("private diagnostic", out)
        self.assertIn("report: ok", out)
        self.assertEqual(len(self.host.treatment_payloads), 1)
        self.assertEqual(self.host.treatment_results, [])
        self.assertEqual(len(self.host.reports), 1)
        payload = self.host.reports[0]
        self.assertEqual(payload["failed_stage"], "treatment")
        self.assertIsNone(payload["treatment"])
        self.assertEqual(payload["state"], "built")
        self.assertIsNone(payload["failure_reason"])
        self.assertIsNone(self.host.lock_holder)

    def test_treatment_interrupt_names_whole_replacement_and_releases_the_lock(self) -> None:
        label = self.fixture.labels[0]
        self.host.interrupt_phase = "treatment"
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("treatment: refuse", out)
        self.assertIn("interrupted; the generation's states are replaced whole by the next run", out)
        self.assertIn(report.command(f"index build caselaw --lockfile {label}"), out)
        self.assertEqual(len(self.host.treatment_payloads), 1)
        self.assertEqual(self.host.treatment_results, [])
        self.assertEqual(self.host.reports, [])
        self.assertIsNone(self.host.lock_holder)
        self.host.interrupt_phase = None
        code, out, err = self.run_build(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("treatment: ok", out)
        self.assertEqual(self.host.reports[-1]["treatment"], self.host.treatment_results[-1].to_json())

    def test_report_write_failure_after_success_refuses_with_a_rerun_fix(self) -> None:
        self.host.report_write_code = 3
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("built: ok", out)
        self.assertIn("report: refuse", out)
        self.assertIn("build report write failed (exit 3)", out)
        self.assertIn(stack.logs_fix(RENDERED, "postgres"), out)
        self.assertIn(report.command("index build caselaw"), out)
        self.assertNotIn("private diagnostic", out)
        self.assertEqual(self.host.reports, [])
        self.assertIsNone(self.host.lock_holder)

    def test_report_interrupt_warns_that_the_row_may_stand_and_releases_the_lock(self) -> None:
        self.host.interrupt_phase = "report"
        code, out, err = self.run_build()
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn("interrupted; the report row may stand", out)
        self.assertIn(report.command("index build caselaw"), out)
        self.assertIsNone(self.host.lock_holder)

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
