"""Corpus install generation decisions and recovery over a fictitious host."""

import ast
import contextlib
import copy
import subprocess
import unittest
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import test_corpus_install
from test_corpus_install import NOW, FakeHost

from gideon.host import models, report
from gideon.host.corpus import install
from gideon.host.corpus.lockfile import Lockfile, load_lockfile, render_lockfile
from gideon.host.corpus.sources import SourceDefinition
from gideon.host.index import generations
from gideon.host.index.record import GenerationRow
from gideon.host.render import worker
from gideon.host.sysio import Command, PathLike


def _interrupt() -> None:
    raise KeyboardInterrupt


@contextlib.contextmanager
def _on_sql(
    host: FakeHost, fragment: str, action: Callable[[], None], *,
    after: bool = False, occurrence: int = 1,
) -> Iterator[None]:
    """Inject a fault at one recorded database statement's boundary."""

    run = host.run
    matches = 0

    def injected(argv: Command, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal matches
        matched = fragment in (kwargs.get("input") or "")
        if matched:
            matches += 1
        selected = matched and matches == occurrence
        if selected and not after:
            action()
        result = run(argv, **kwargs)
        if selected and after:
            action()
        return result

    with patch.object(host, "run", side_effect=injected):
        yield


class InstallBuild(unittest.TestCase):
    """Collection verdicts, the installed move, and the work directory's removal."""

    def setUp(self) -> None:
        self.fixture = self._new_fixture()
        self.host: FakeHost = self.fixture.host

    def _new_fixture(self) -> test_corpus_install.Install:
        fixture = test_corpus_install.Install()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def _lockfile(self, fixture: test_corpus_install.Install, label: str) -> Lockfile:
        loaded = load_lockfile(
            fixture.checkout / "corpus/lockfiles" / f"{label}.yaml",
            known_sources={fixture.source.name: fixture.source},
        )
        self.assertTrue(loaded.ok)
        assert loaded.lockfile is not None
        return loaded.lockfile

    def _seed_generation(
        self, fixture: test_corpus_install.Install, state: str, *, label: str | None = None,
        finished_report: bool = True,
    ) -> GenerationRow:
        lockfile = self._lockfile(fixture, label or fixture.labels[0])
        loaded = models.load_models_lock(fixture.checkout / "models.lock")
        self.assertTrue(loaded.ok, models.render_errors(loaded.errors))
        assert loaded.lock is not None
        profile = models.select_profile(loaded.lock, loaded.lock.reference)
        assert isinstance(profile, models.HardwareProfile) and profile.embedding_space is not None
        collection = generations.COLLECTIONS[0].name
        space = profile.embedding_space.id
        finished = NOW.isoformat() if state in {"built", "live", "retired"} else None
        row = GenerationRow(
            generations.generation_name(collection, lockfile.label, space),
            collection, lockfile.label, (lockfile.label,), lockfile.pipeline, space,
            generations.inputs_hash(lockfile, collection, space, sources=(fixture.source,)),
            state, "store" if state == "failed" else None,
            {**fixture.host.counts, **fixture.host.dense_counts}, 4096,
            finished, finished, finished,
            finished if state in {"live", "retired"} else None,
            finished if state == "retired" else None, NOW.isoformat(), clustered_at=finished,
        )
        fixture.host.generations[row.gen_id] = row
        if state == "built" and finished_report:
            fixture.host.seed_finished_report(row)
        return row

    def _assert_released(self, host: FakeHost) -> None:
        self.assertIsNone(host.lock_holder)
        self.assertEqual(host.lock_releases, 1)

    def _assert_refusal(self, result: tuple[int, str, str], stage: str, detail: str) -> str:
        code, out, err = result
        self.assertEqual((code, err), (1, ""), out)
        self.assertIn(f"{stage}: refuse", out)
        self.assertIn(detail, out)
        self.assertIn(report.command("corpus install"), out)
        self.assertNotIn("retain:", out)
        self.assertNotIn("Next:", out)
        return out

    def _assert_finished_install(
        self, row: GenerationRow, result: tuple[int, str, str],
        deferred_before: list[dict[str, Any]],
    ) -> str:
        code, out, err = result
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(
            f"collections: ok — {row.collection}: unchanged; generation {row.gen_id} built, "
            f"its treatment and report to finish, inputs hash {row.inputs_hash}", out,
        )
        self.assertIn("collections: ok — 0 of 1 need a build, 1 to finish", out)
        self.assertIn(f"generation: ok — {row.gen_id}: built, found", out)
        self.assertIn(f"treatment: ok — {row.gen_id}:", out)
        self.assertIn(f"report: ok — {row.gen_id}:", out)
        self.assertLess(out.index("treatment: ok"), out.index("report: ok"))
        self.assertLess(out.index("report: ok"), out.index("installed: ok"))
        for stage in ("cluster", "build", "counts", "dense", "built"):
            self.assertFalse(any(line.startswith(f"{stage}:") for line in out.splitlines()), out)
        self.assertEqual(self.host.deferred, deferred_before)
        self.assertEqual(self.host.generations[row.gen_id], row)
        self.assertIsNone(self.host.reports[-1]["failed_stage"])
        self.assertEqual(self.host.reports[-1]["treatment"], self.host.treatment_results[-1].to_json())
        self.assertFalse(self.host.reports[-1]["wall_clock"]["whole_build"])
        self.assertEqual(self.host.record_rows[self.fixture.labels[0]]["state"], "installed")
        self.assertFalse((self.fixture.work / self.fixture.labels[0]).exists())
        self.assertIsNone(self.host.lock_holder)
        return out

    def test_built_generation_without_a_report_finishes_without_build_jobs(self) -> None:
        row = self._seed_generation(self.fixture, "built", finished_report=False)
        self.assertEqual(self.host.reports, [])
        self._assert_finished_install(row, self.fixture.run_install(), [])
        self.assertEqual(self.host.jobs, {})
        self.assertEqual(len(self.host.reports), 1)
        self._assert_released(self.host)

    def test_built_generation_with_a_newest_failed_treatment_report_is_finished(self) -> None:
        row = self._seed_generation(self.fixture, "built")
        failed = self.host.seed_finished_report(row)
        failed.update(failed_stage="treatment", treatment=None)
        previous = copy.deepcopy(self.host.reports)
        self._assert_finished_install(row, self.fixture.run_install(), [])
        self.assertEqual(len(self.host.reports), 3)
        self.assertEqual(self.host.reports[:-1], previous)
        self.assertEqual(failed["failed_stage"], "treatment")
        self._assert_released(self.host)

    def test_built_generation_reported_before_the_treatment_stage_is_finished(self) -> None:
        row = self._seed_generation(self.fixture, "built")
        self.host.reports[-1].update(treatment=None)
        previous = copy.deepcopy(self.host.reports)
        self._assert_finished_install(row, self.fixture.run_install(), [])
        self.assertEqual(self.host.reports[:-1], previous)
        self.assertIsNone(previous[-1]["failed_stage"])
        self._assert_released(self.host)

    def test_built_generation_with_a_newest_finished_report_stays_unchanged(self) -> None:
        row = self._seed_generation(self.fixture, "built")
        self.host.reports[-1].update(failed_stage="treatment", treatment=None)
        completed = self.host.seed_finished_report(row)
        code, out, err = self.fixture.run_install()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(
            f"collections: ok — {row.collection}: unchanged; generation {row.gen_id} built, "
            f"inputs hash {row.inputs_hash}", out,
        )
        self.assertIn("collections: ok — 0 of 1 need a build", out)
        self.assertNotIn("to finish", out)
        stages = [line.split(":", 1)[0] for line in out.splitlines()]
        self.assertNotIn("treatment", stages)
        self.assertNotIn("report", stages)
        self.assertEqual(self.host.treatment_payloads, [])
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.generations[row.gen_id], row)
        self.assertEqual(len(self.host.reports), 2)
        self.assertEqual(self.host.reports[-1], completed)
        self.assertEqual(self.host.record_rows[self.fixture.labels[0]]["state"], "installed")
        self._assert_released(self.host)

    def test_live_generation_without_a_report_is_never_finished(self) -> None:
        row = self._seed_generation(self.fixture, "live")
        self.assertEqual(self.host.reports, [])
        code, out, err = self.fixture.run_install()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"unchanged; generation {row.gen_id} live, inputs hash {row.inputs_hash}", out)
        self.assertNotIn("to finish", out)
        stages = [line.split(":", 1)[0] for line in out.splitlines()]
        self.assertNotIn("treatment", stages)
        self.assertNotIn("report", stages)
        self.assertEqual(self.host.treatment_payloads, [])
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.reports, [])
        self.assertEqual(self.host.generations[row.gen_id], row)
        self.assertEqual(self.host.record_rows[self.fixture.labels[0]]["state"], "installed")
        self._assert_released(self.host)

    def test_treatment_failure_while_finishing_keeps_the_label_installing(self) -> None:
        row = self._seed_generation(self.fixture, "built", finished_report=False)
        self.host.fail_phase = "treatment"
        out = self._assert_refusal(self.fixture.run_install(), "treatment", "exit 3")
        self.assertIn("0 of 1 need a build, 1 to finish", out)
        self.assertIn(f"report: ok — {row.gen_id}:", out)
        self.assertLess(out.index("treatment: refuse"), out.index("report: ok"))
        self.assertNotIn("installed:", out)
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.generations[row.gen_id], row)
        self.assertEqual(len(self.host.reports), 1)
        self.assertEqual(self.host.reports[0]["failed_stage"], "treatment")
        self.assertIsNone(self.host.reports[0]["treatment"])
        self.assertEqual(self.host.record_rows[self.fixture.labels[0]]["state"], "installing")
        self.assertIsNone(self.host.record_rows[self.fixture.labels[0]]["installed_at"])
        self._assert_released(self.host)

    def _assert_first_install_failure_is_finished_on_retry(self, stage: str) -> None:
        if stage == "treatment":
            self.host.fail_phase = stage
        else:
            self.host.report_write_code = 3
        out = self._assert_refusal(self.fixture.run_install(), stage, "exit 3")
        row = next(iter(self.host.generations.values()))
        self.assertEqual(row.state, "built")
        self.assertIn("built: ok", out)
        self.assertNotIn("installed:", out)
        self.assertEqual(self.host.record_rows[self.fixture.labels[0]]["state"], "installing")
        self.assertIsNone(self.host.record_rows[self.fixture.labels[0]]["installed_at"])
        self.assertTrue((self.fixture.work / self.fixture.labels[0]).exists())
        if stage == "treatment":
            self.assertEqual(len(self.host.reports), 1)
            self.assertEqual(self.host.reports[0]["failed_stage"], stage)
            self.assertIsNone(self.host.reports[0]["treatment"])
        else:
            self.assertEqual(self.host.reports, [])
        self._assert_released(self.host)
        deferred_before = list(self.host.deferred)
        self.assertEqual(set(self.host.task_by_job.values()), {
            worker.DUPES_TASK, worker.LEXICAL_TASK, worker.DENSE_TASK,
        })
        treatment_attempts = len(self.host.treatment_payloads)
        reports = list(self.host.reports)
        self.host.fail_phase = None
        self.host.report_write_code = 0
        self._assert_finished_install(row, self.fixture.run_install(), deferred_before)
        self.assertEqual(len(self.host.treatment_payloads), treatment_attempts + 1)
        self.assertEqual(self.host.reports[:-1], reports)
        self.assertEqual(self.host.lock_releases, 2)

    def test_install_retries_a_failed_treatment_without_rebuilding(self) -> None:
        self._assert_first_install_failure_is_finished_on_retry("treatment")

    def test_install_retries_a_failed_report_write_without_rebuilding(self) -> None:
        self._assert_first_install_failure_is_finished_on_retry("report")

    def test_unchanged_built_and_live_collections_defer_no_build_jobs(self) -> None:
        for state in ("built", "live"):
            with self.subTest(state=state):
                fixture = self._new_fixture()
                host: FakeHost = fixture.host
                row = self._seed_generation(fixture, state)
                code, out, err = fixture.run_install()
                self.assertEqual((code, err), (0, ""), out)
                self.assertIn(
                    f"collections: ok — {row.collection}: unchanged; generation "
                    f"{row.gen_id} {state}, inputs hash {row.inputs_hash}", out,
                )
                self.assertIn("collections: ok — 0 of 1 need a build", out)
                self.assertIn(f"generation: ok — {row.gen_id}: {state}, found", out)
                self.assertEqual(host.deferred, [])
                self.assertEqual(host.jobs, {})
                self.assertNotIn("build:", out)
                self.assertNotIn("dense:", out)
                self.assertEqual(host.generations[row.gen_id], row)
                self.assertEqual(host.record_rows[fixture.labels[0]]["state"], "installed")
                self._assert_released(host)

    def test_changed_pipeline_names_and_builds_a_new_generation(self) -> None:
        first, second = self.fixture.labels
        old = self._seed_generation(self.fixture, "built")
        self.fixture._hold_record("installed")
        self.fixture._derive_second_label()
        lockfile = self._lockfile(self.fixture, second)
        pipeline = "1000.0.0"
        changed = replace(lockfile, pipeline=pipeline, reason="pipeline")
        (self.fixture.checkout / "corpus/lockfiles" / f"{second}.yaml").write_text(render_lockfile(changed))
        expected_name = generations.generation_name(old.collection, second, old.embedding_space)
        with patch.dict(worker.PIPELINE_CHUNK_SIZES, {pipeline: worker.PIPELINE_CHUNK_SIZES[old.pipeline]}):
            expected_hash = generations.inputs_hash(changed, old.collection, old.embedding_space,
                                                    sources=(self.fixture.source,))
            code, out, err = self.fixture.run_install(second)
        self.assertEqual((code, err), (0, ""), out)
        self.assertNotEqual(expected_hash, old.inputs_hash)
        self.assertIn(
            f"needs build; generation {expected_name}, inputs hash {expected_hash}", out,
        )
        self.assertEqual(set(self.host.generations), {old.gen_id, expected_name})
        self.assertEqual(self.host.generations[old.gen_id], old)
        new = self.host.generations[expected_name]
        self.assertEqual((new.pipeline, new.inputs_hash, new.state), (pipeline, expected_hash, "built"))
        self.assertEqual([job["name"] for job in self.host.deferred], [expected_name, expected_name, expected_name])
        stages = [line.split(":", 1)[0] for line in out.splitlines()]
        self.assertLess(stages.index("cluster"), stages.index("build"))
        self.assertLess(stages.index("built"), stages.index("treatment"))
        self.assertLess(stages.index("treatment"), stages.index("report"))
        self.assertLess(stages.index("report"), stages.index("installed"))
        self.assertIn(f"cluster: ok — {expected_name}:", out)
        self.assertIn(f"treatment: ok — {expected_name}:", out)
        self.assertIn(f"report: ok — {expected_name}:", out)
        self.assertTrue(all(job["pipeline"] == pipeline for job in self.host.deferred))
        self.assertEqual(self.host.record_rows[first]["state"], "superseded")
        self.assertEqual(self.host.record_rows[second]["state"], "installed")

    def test_treatment_uses_the_selected_lockfiles_capture_or_none(self) -> None:
        for has_capture in (False, True):
            with self.subTest(has_capture=has_capture):
                fixture = self._new_fixture()
                sources: tuple[SourceDefinition, ...] = (fixture.source,)
                if has_capture:
                    captured, _, _ = fixture._add_captured_source()
                    sources += (captured,)
                label = fixture.labels[-1]
                loaded = load_lockfile(
                    fixture.checkout / "corpus/lockfiles" / f"{label}.yaml",
                    known_sources={source.name: source for source in sources},
                )
                self.assertTrue(loaded.ok)
                assert loaded.lockfile is not None
                pin = loaded.lockfile.sources.get(worker.OVERRULED_SOURCE)
                expected = (worker.OVERRULED_SOURCE, pin.snapshot_date) if pin else (None, None)
                snapshot = f"{expected[0]}-{expected[1]}" if pin else None
                code, out, err = fixture.run_install(label, sources=sources)
                self.assertEqual((code, err), (0, ""), out)
                self.assertEqual(len(fixture.host.treatment_payloads), 1)
                payload = fixture.host.treatment_payloads[0]
                self.assertEqual((payload["treatment_source"], payload["treatment_snapshot_date"]), expected)
                self.assertEqual(len(fixture.host.treatment_results), 1)
                materialized = fixture.host.treatment_results[0]
                self.assertEqual(materialized.treatment_snapshot, snapshot)
                self.assertEqual(fixture.host.reports[0]["treatment"], materialized.to_json())
                self.assertIn(f"treatment: ok — {payload['gen_id']}:", out)
                self.assertLess(out.index("built: ok"), out.index("treatment: ok"))
                self.assertLess(out.index("treatment: ok"), out.index("report: ok"))

    def test_building_and_failed_generations_resume_as_needs_build(self) -> None:
        for state in ("building", "failed"):
            with self.subTest(state=state):
                fixture = self._new_fixture()
                row = self._seed_generation(fixture, state)
                code, out, err = fixture.run_install()
                self.assertEqual((code, err), (0, ""), out)
                reason = f": {row.failure_reason}" if row.failure_reason is not None else ""
                self.assertIn(
                    f"needs build; generation {row.gen_id} {state}{reason}, inputs hash {row.inputs_hash}", out,
                )
                self.assertIn("collections: ok — 1 of 1 need a build", out)
                self.assertIn(f"generation: ok — {row.gen_id}: {state}, found", out)
                self.assertEqual([job["name"] for job in fixture.host.deferred], [row.gen_id, row.gen_id, row.gen_id])
                self.assertEqual(fixture.host.generations[row.gen_id].state, "built")
                self.assertIsNone(fixture.host.generations[row.gen_id].failure_reason)
                self._assert_released(fixture.host)

    def test_retired_generation_refuses_with_both_ways_out_and_builds_nothing(self) -> None:
        row = self._seed_generation(self.fixture, "retired")
        out = self._assert_refusal(self.fixture.run_install(), "collections", "generation is retired")
        self.assertIn(report.command("corpus cut"), out)
        self.assertIn(report.command("index gc"), out)
        self.assertIn("once its hold ends", out)
        self.assertEqual(self.host.deferred, [])
        self.assertEqual(self.host.generations[row.gen_id], row)
        self.assertNotIn("generation:", out)
        self.assertNotIn("clean:", out)
        self.assertEqual(self.host.record_rows[self.fixture.labels[0]]["state"], "installing")
        self._assert_released(self.host)

    def test_found_generation_appends_the_label_and_keeps_its_original_name(self) -> None:
        first, second = self.fixture.labels
        row = self._seed_generation(self.fixture, "built")
        self.fixture._hold_record("installed")
        self.fixture._derive_second_label()
        code, out, err = self.fixture.run_install(second)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"generation: ok — {row.gen_id}: built, found", out)
        self.assertEqual(self.host.generations[row.gen_id].lockfiles, (first, second))
        self.assertEqual(self.host.generations[row.gen_id].corpus_lockfile, first)
        self.assertEqual(set(self.host.generations), {row.gen_id})
        self.assertEqual(self.host.deferred, [])
        self.assertIn(f"Next: run {report.command(f'index promote {row.gen_id}')} to serve it.", out)

    def test_installed_move_supersedes_earlier_labels_and_keeps_the_previous_timestamp(self) -> None:
        previous, abandoned = self.fixture.labels
        label = self.fixture._add_third_lockfile()
        self.fixture._hold_record("installed", previous)
        previous_at = (NOW - timedelta(days=1)).isoformat()
        self.host.record_rows[previous]["installed_at"] = previous_at
        self.fixture._hold_record("installing", abandoned)
        code, out, err = self.fixture.run_install(label)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(
            f"installed: ok — {label}: installed at {NOW.isoformat()}; "
            f"superseded {previous}, {abandoned}", out,
        )
        self.assertEqual(self.host.record_rows[label]["state"], "installed")
        self.assertEqual(self.host.record_rows[label]["installed_at"], NOW.isoformat())
        self.assertEqual(self.host.record_rows[previous]["state"], "superseded")
        self.assertEqual(self.host.record_rows[previous]["installed_at"], previous_at)
        self.assertEqual(self.host.record_rows[abandoned]["state"], "superseded")
        self.assertIsNone(self.host.record_rows[abandoned]["installed_at"])

    def test_installed_move_leaves_a_later_installing_label_for_its_own_install(self) -> None:
        label, later = self.fixture.labels
        self.fixture._hold_record("installing", later)
        code, out, err = self.fixture.run_install()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"installed: ok — {label}: installed at {NOW.isoformat()}; none superseded", out)
        self.assertEqual(self.host.record_rows[label]["state"], "installed")
        self.assertEqual(self.host.record_rows[later]["state"], "installing")

    def test_cut_and_missing_labels_are_refused_inside_the_installed_move(self) -> None:
        for state, detail in (("cut", "is not installing"), ("missing", "is not recorded")):
            with self.subTest(state=state):
                fixture = self._new_fixture()
                label, previous = fixture.labels
                fixture._hold_record("installed", previous)
                previous_row = dict(fixture.host.record_rows[previous])

                def change_label(
                    fixture: test_corpus_install.Install = fixture, label: str = label, state: str = state,
                ) -> None:
                    if state == "missing":
                        fixture.host.record_rows.pop(label)
                    else:
                        fixture.host.record_rows[label]["state"] = state

                with _on_sql(fixture.host, "DO $installed$", change_label):
                    out = self._assert_refusal(fixture.run_install(), "installed", f"lockfile label {label} {detail}")
                self.assertIn("built: ok", out)
                self.assertNotIn("clean:", out)
                self.assertEqual(fixture.host.record_rows[previous], previous_row)
                self.assertTrue((fixture.work / label).exists())
                if state == "missing":
                    self.assertNotIn(label, fixture.host.record_rows)
                else:
                    self.assertEqual(fixture.host.record_rows[label]["state"], state)
                    self.assertIsNone(fixture.host.record_rows[label]["installed_at"])
                self._assert_released(fixture.host)

    def test_unfinished_generation_refuses_the_move_and_leaves_the_label_installing(self) -> None:
        self.host.dense_finish_stamp = None
        self.host.dense_counts["dense_limit"] = 1
        out = self._assert_refusal(self.fixture.run_install(), "installed", "is building")
        self.assertIn("build: ok", out)
        self.assertIn("dense: ok", out)
        self.assertIn("built: ok", out)
        self.assertEqual(len(self.host.deferred), 3)
        self.assertEqual(self.host.record_rows[self.fixture.labels[0]]["state"], "installing")
        self.assertIsNone(self.host.record_rows[self.fixture.labels[0]]["installed_at"])
        self.assertNotIn("clean:", out)
        self.assertTrue((self.fixture.work / self.fixture.labels[0]).exists())
        self._assert_released(self.host)

    def test_clean_removes_the_present_label_directory_before_retaining(self) -> None:
        directory = self.fixture.work / self.fixture.labels[0]
        code, out, err = self.fixture.run_install()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"clean: ok — removed {directory}", out)
        self.assertIn(directory, self.host.removed_dirs)
        self.assertFalse(directory.exists())
        self.assertLess(out.index("installed: ok"), out.index("clean: ok"))
        self.assertLess(out.index("clean: ok"), out.index("retain: ok"))

    def test_clean_with_an_absent_directory_removes_nothing(self) -> None:
        self.fixture._hold_record("installed")
        self._seed_generation(self.fixture, "built")
        directory = self.fixture.work / self.fixture.labels[0]
        code, out, err = self.fixture.run_install()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn(f"clean: ok — {directory} is absent; nothing removed", out)
        self.assertEqual(self.host.removed_dirs, [])
        self.assertFalse(directory.exists())

    def test_interrupted_cleanup_is_completed_by_the_next_install(self) -> None:
        label = self.fixture.labels[0]
        directory = self.fixture.work / label
        removed: list[Path] = []
        rmtree = self.host.rmtree

        def partial_removal(path: PathLike) -> None:
            if Path(path) == directory:
                child = next(item for item in directory.rglob("*") if item.is_file())
                child.unlink()
                removed.append(child)
                raise KeyboardInterrupt
            rmtree(path)

        with patch.object(self.host, "rmtree", side_effect=partial_removal):
            out = self._assert_refusal(self.fixture.run_install(), "clean", "removed whole by the next run")
        self.assertIn("installed: ok", out)
        self.assertEqual(self.host.record_rows[label]["state"], "installed")
        self.assertTrue(directory.exists())
        self.assertEqual(len(removed), 1)
        self.assertFalse(removed[0].exists())
        code, rerun, err = self.fixture.run_install()
        self.assertEqual((code, err), (0, ""), rerun)
        self.assertIn(f"clean: ok — removed {directory}", rerun)
        self.assertFalse(directory.exists())
        self.assertIn(directory, self.host.removed_dirs)
        self.assertEqual(self.host.lock_releases, 2)

    def test_cleanup_access_failures_refuse_after_installation(self) -> None:
        for operation in ("listdir", "rmtree"):
            with self.subTest(operation=operation):
                fixture = self._new_fixture()
                label = fixture.labels[0]
                directory = fixture.work / label
                fault: Callable[[PathLike], object]
                if operation == "listdir":
                    listdir = fixture.host.listdir

                    def unreadable(
                        path: PathLike, root: Path = fixture.work,
                        listdir: Callable[[PathLike], list[str]] = listdir,
                    ) -> list[str]:
                        if Path(path) == root:
                            raise OSError("fictitious work access failure")
                        return listdir(path)

                    fault = unreadable
                else:
                    rmtree = fixture.host.rmtree

                    def inaccessible(
                        path: PathLike, directory: Path = directory,
                        rmtree: Callable[[PathLike], None] = rmtree,
                    ) -> None:
                        if Path(path) == directory:
                            raise OSError("fictitious removal failure")
                        rmtree(path)

                    fault = inaccessible
                with patch.object(fixture.host, operation, side_effect=fault):
                    out = self._assert_refusal(fixture.run_install(), "clean", "OSError")
                self.assertIn("Restore access to", out)
                self.assertEqual(fixture.host.record_rows[label]["state"], "installed")
                self.assertEqual(fixture.host.record_rows[label]["installed_at"], NOW.isoformat())
                self.assertTrue(directory.exists())
                self.assertNotIn(directory, fixture.host.removed_dirs)
                self._assert_released(fixture.host)

    def test_interrupt_after_installed_transaction_recovers_without_worker_jobs(self) -> None:
        source, _, _ = self.fixture._add_captured_source()
        sources = (self.fixture.source, source)
        label = self.fixture.labels[0]
        directory = self.fixture.work / label
        with _on_sql(self.host, "DO $installed$", _interrupt, after=True):
            out = self._assert_refusal(self.fixture.run_install(sources=sources), "installed", "one transaction")
        self.assertNotIn("clean:", out)
        self.assertEqual(self.host.record_rows[label]["state"], "installed")
        installed_at = self.host.record_rows[label]["installed_at"]
        self.assertEqual(installed_at, NOW.isoformat())
        self.assertTrue(directory.exists())
        deferred = [list(jobs) for jobs in (
            self.host.stage_deferred, self.host.caselaw_deferred, self.host.overruled_deferred,
            self.host.agreement_deferred, self.host.deferred,
        )]
        self.assertTrue(all(deferred))
        code, rerun, err = self.fixture.run_install(sources=sources, clock=lambda: NOW + timedelta(days=1))
        self.assertEqual((code, err), (0, ""), rerun)
        for stage in ("stage", "ingest", "overruled", "agreement"):
            self.assertIn(f"{stage}: ok — {label} is installed; nothing to do", rerun)
        self.assertEqual([self.host.stage_deferred, self.host.caselaw_deferred,
                          self.host.overruled_deferred, self.host.agreement_deferred, self.host.deferred], deferred)
        self.assertIn(f"installed: ok — {label}: installed (kept); none superseded", rerun)
        self.assertIn(f"clean: ok — removed {directory}", rerun)
        self.assertEqual(self.host.record_rows[label]["installed_at"], installed_at)
        self.assertFalse(directory.exists())
        self.assertEqual(self.host.lock_releases, 2)

    def test_installed_skip_rows_still_run_cleanup(self) -> None:
        label = self.fixture.labels[0]
        self.fixture._hold_record("installed")
        self._seed_generation(self.fixture, "built")
        directory = self.fixture.work / label
        directory.mkdir()
        (directory / "fictitious-leftover.txt").write_text("Fictitious interrupted work.\n")
        code, out, err = self.fixture.run_install(clock=lambda: NOW + timedelta(days=1))
        self.assertEqual((code, err), (0, ""), out)
        for stage in ("stage", "ingest", "overruled", "agreement"):
            self.assertIn(f"{stage}: ok — {label} is installed; nothing to do", out)
        self.assertEqual(self.host.stage_deferred, [])
        self.assertEqual(self.host.caselaw_deferred, [])
        self.assertEqual(self.host.overruled_deferred, [])
        self.assertEqual(self.host.agreement_deferred, [])
        self.assertEqual(self.host.deferred, [])
        self.assertIn(f"clean: ok — removed {directory}", out)
        self.assertFalse(directory.exists())
        self.assertEqual(self.host.record_rows[label]["installed_at"], NOW.isoformat())

    def test_next_line_names_built_generations_and_omits_live_ones(self) -> None:
        for state in ("built", "live"):
            with self.subTest(state=state):
                fixture = self._new_fixture()
                row = self._seed_generation(fixture, state)
                code, out, err = fixture.run_install()
                self.assertEqual((code, err), (0, ""), out)
                next_lines = [line for line in out.splitlines() if line.startswith("Next:")]
                expected = [f"Next: run {report.command(f'index promote {row.gen_id}')} to serve it."]
                self.assertEqual(next_lines, expected if state == "built" else [])
                if next_lines:
                    self.assertLess(out.index("retain: ok"), out.index("Next:"))

    def test_each_new_interrupt_reports_recovery_and_releases_the_lock(self) -> None:
        details = {
            "collections": "the verdicts stand", "generation": "the row stands",
            "cluster": "the clustering continues in the worker",
            "build": "the build continues in the worker", "counts": "the row stands",
            "dense": "the embed continues in the worker", "built": "the row stands",
            "treatment": "the generation's states are replaced whole by the next run",
            "report": "the report row may stand",
            "installed": "one transaction", "clean": "removed whole by the next run",
        }
        for stage, detail in details.items():
            with self.subTest(stage=stage):
                fixture = self._new_fixture()
                host: FakeHost = fixture.host
                label = fixture.labels[0]
                fault: contextlib.AbstractContextManager[object]
                if stage == "generation":
                    fault = _on_sql(host, "DO $generation$", _interrupt, after=True)
                elif stage == "built":
                    # The name reads cover cluster, lexical counts, dense completion, then built.
                    fault = _on_sql(host, "WHERE gen_id = :'v_name'", _interrupt, occurrence=4)
                elif stage == "installed":
                    fault = _on_sql(host, "DO $installed$", _interrupt)
                elif stage == "clean":
                    listdir = host.listdir

                    def interrupted_listing(
                        path: PathLike, root: Path = fixture.work,
                        listdir: Callable[[PathLike], list[str]] = listdir,
                    ) -> list[str]:
                        if Path(path) == root:
                            raise KeyboardInterrupt
                        return listdir(path)

                    fault = patch.object(host, "listdir", side_effect=interrupted_listing)
                else:
                    host.interrupt_phase = stage
                    fault = contextlib.nullcontext()
                with fault:
                    out = self._assert_refusal(fixture.run_install(), stage, detail)
                self.assertIn("interrupted;", out)
                self.assertEqual(host.record_rows[label]["state"], "installed" if stage == "clean" else "installing")
                if stage in {"cluster", "build", "dense"}:
                    self.assertIn("again to rejoin it", out)
                    self.assertTrue(host.deferred)
                if stage == "generation":
                    self.assertEqual(len(host.generations), 1)
                    self.assertEqual(host.deferred, [])
                if stage in {"built", "treatment", "report"}:
                    self.assertEqual(next(iter(host.generations.values())).state, "built")
                self.assertTrue((fixture.work / label).exists())
                self._assert_released(host)

    def test_install_imports_and_calls_no_promote_module_or_command(self) -> None:
        tree = ast.parse(Path(install.__file__).read_text())
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
                modules.extend(alias.name for alias in node.names)
        self.assertFalse(any(name.split(".")[-1] == "promote" for name in modules))
        code, out, err = self.fixture.run_install()
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("Next: run", out)
        self.assertFalse(any("index promote" in " ".join(argv) for argv in self.host.calls))
