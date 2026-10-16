"""Contracts for the evaluation slice registry's call surfaces."""

import ast
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

from test_evaluation_record import read_psql_set
from test_evaluation_run import (
    NOW,
    ROOT,
    EvalHost,
    _generation_row,
    _invoke,
    _run_kwargs,
)
from test_judgments_slice import _fixture, _ranked_file

from gideon.evaluation import command
from gideon.evaluation.evalset import SET_ROOT, LoadedSet
from gideon.evaluation.results import RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS, CallSurface
from gideon.host import report
from gideon.host.render.ci import CI_STACK, PRODUCTION_STACK


class SliceSurfaces(unittest.TestCase):
    """Each registry entry names the surfaces its runs reach."""

    def test_every_entry_names_its_surfaces_from_the_closed_list(self) -> None:
        for name, spec in SLICE_RUNNERS.items():
            with self.subTest(slice_name=name):
                self.assertIs(type(spec.surfaces), frozenset)
                self.assertTrue(all(type(surface) is CallSurface for surface in spec.surfaces))

    def test_reaches_box_matches_surface_membership(self) -> None:
        on_box = {surface for surface in CallSurface if surface.on_box}
        self.assertEqual(on_box, {
            CallSurface.ENGINE, CallSurface.IMAGE, CallSurface.RECORD,
            CallSurface.STORES, CallSurface.TURNS,
        })
        for name, spec in SLICE_RUNNERS.items():
            with self.subTest(slice_name=name):
                self.assertIs(type(spec.reaches_box), bool)
                self.assertEqual(spec.reaches_box, bool(spec.surfaces & on_box))

    def test_turns_without_the_engine_is_refused_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "turns surface must name the engine surface"):
            replace(SLICE_RUNNERS["extraction"], surfaces=frozenset({CallSurface.TURNS}))

    def test_replacing_a_runner_keeps_the_surfaces(self) -> None:
        for name, original in SLICE_RUNNERS.items():
            with self.subTest(slice_name=name):
                runner = SLICE_RUNNERS["extraction"].runner
                changed = replace(original, runner=runner)
                self.assertIs(changed.runner, runner)
                self.assertEqual(changed.surfaces, original.surfaces)


def _admissible_surfaces() -> tuple[frozenset[CallSurface], ...]:
    """Every set of surfaces a registry entry can be constructed with."""

    admissible = []
    for size in range(len(CallSurface) + 1):
        for chosen in combinations(CallSurface, size):
            try:
                replace(SLICE_RUNNERS["judgments"], surfaces=frozenset(chosen))
            except ValueError:
                continue
            admissible.append(frozenset(chosen))
    return tuple(admissible)


def _invoke_planted(
    host: EvalHost,
    checkout: Path,
    ranked_path: Path,
    surfaces: frozenset[CallSurface],
    *,
    supplied_set: Path | None = None,
    site_path: Path | None = None,
    stack_name: str = PRODUCTION_STACK,
    generation_name: str | None = None,
    generation_reply: str | None = None,
    flags: tuple[str, ...] = (),
) -> tuple[tuple[int, str, str], list[RunContext], command.engine.EngineTarget]:
    """Run one surface set through the command with observable seams."""

    contexts: list[RunContext] = []
    host.generation_output = (
        json.dumps(_generation_row()) + "\n"
        if generation_reply is None else generation_reply
    )

    def run_slice(_loaded: LoadedSet, _name: str, context: RunContext) -> SliceResult:
        contexts.append(context)
        return SliceResult(True, "visibly fictitious runner report\n", ())

    target = command.engine.EngineTarget("fictitious-target-profile", "fictitious-model", 1)
    replacement = dict(SLICE_RUNNERS)
    replacement["judgments"] = replace(
        replacement["judgments"], surfaces=surfaces, runner=run_slice
    )
    argv = ["eval", "run", "--slice", "judgments", "--stack", stack_name, *flags]
    if generation_name is not None:
        argv.extend(("--generation", generation_name))
    if CallSurface.RANKED_FILE in surfaces:
        argv.extend(("--ranked", str(ranked_path)))
    if supplied_set is not None:
        argv.extend(("--set", str(supplied_set)))
    kwargs = _run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml")
    kwargs["clock"] = lambda: NOW
    if site_path is not None:
        kwargs["site_path"] = site_path
    with (
        patch.object(command, "SLICE_RUNNERS", replacement),
        patch.object(command.engine, "resolve_engine_target", return_value=target),
        patch.object(command.access, "read_eval_password", return_value="fictitious-password"),
        patch.object(
            command.access, "make_client_factory", return_value=lambda: object()
        ),
        patch.object(
            command.door,
            "probe",
            return_value=command.door.ProbeResult(True, "door ready", None),
        ),
        patch.object(command.stacks.secrets, "select_directory") as select_directory,
    ):
        outcome = _invoke(argv, **kwargs)
        if stack_name == CI_STACK:
            select_directory.assert_called_once()
        else:
            select_directory.assert_not_called()
    return outcome, contexts, target


class PreparationRoutes(unittest.TestCase):
    """Each named surface supplies its runner context and record facts."""

    def test_record_only_requires_root_and_site_without_a_surface_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3})
            ranked_path, _ = _ranked_file(directory, {1: (1,)})
            supplied_set = Path(directory) / (checkout / SET_ROOT).name
            shutil.copytree(checkout / SET_ROOT, supplied_set)
            record_only = frozenset({CallSurface.RECORD})

            host = EvalHost()
            (code, stdout, stderr), contexts, _ = _invoke_planted(
                host, checkout, ranked_path, record_only, supplied_set=supplied_set
            )
            self.assertEqual((code, stderr), (0, ""), stdout)
            self.assertEqual(len(contexts), 1)
            self.assertIsNone(contexts[0].image)
            self.assertIn("preconditions: ok — root, site, profile ", stdout)
            self.assertNotIn("image present", stdout)
            self.assertFalse(any(input == "SELECT 1;\n" for _, input in host.calls))
            self.assertFalse(any(argv[:3] == ("docker", "image", "inspect") for argv, _ in host.calls))

            (code, stdout, stderr), contexts, _ = _invoke_planted(
                EvalHost(effective_uid=1), checkout, ranked_path, record_only,
                supplied_set=supplied_set,
            )
            self.assertEqual((code, stderr, contexts), (1, "", []))
            self.assertIn("preconditions: refuse — root privileges are required", stdout)

            broken_site = Path(directory) / "broken-site.yaml"
            broken_site.write_text("broken: [\n", encoding="utf-8")
            (code, stdout, stderr), contexts, _ = _invoke_planted(
                EvalHost(), checkout, ranked_path, record_only,
                supplied_set=supplied_set, site_path=broken_site,
            )
            self.assertEqual((code, stderr, contexts), (1, "", []))
            self.assertIn("preconditions: refuse", stdout)

    def test_image_mirror_fix_uses_the_run_form(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            for installed in (False, True):
                with self.subTest(installed=installed):
                    environment = (
                        {report.GIDEON_INSTALLED_COMMAND: "/fictitious/gideon"}
                        if installed else {}
                    )
                    with patch.dict(os.environ, environment, clear=True):
                        host = EvalHost()
                        host.image_probe_rc = 1
                        (code, stdout, stderr), contexts, _target = _invoke_planted(
                            host, checkout, ranked_path, frozenset({CallSurface.IMAGE})
                        )
                    command_text = "gideon registry mirror" if installed else "python3 -m gideon registry mirror"
                    self.assertEqual((code, stderr, contexts), (1, "", []))
                    self.assertIn(
                        f"Fix: Run {command_text}, then retry.", stdout
                    )
                    self.assertNotIn("sudo python3 -m gideon registry mirror", stdout)

                    with (
                        patch.dict(os.environ, environment, clear=True),
                        patch.object(EvalHost, "run", side_effect=OSError("probe unavailable")),
                    ):
                        (code, stdout, stderr), contexts, _target = _invoke_planted(
                            EvalHost(), checkout, ranked_path, frozenset({CallSurface.IMAGE})
                        )
                    self.assertEqual((code, stderr, contexts), (1, "", []))
                    self.assertIn("image presence could not be checked", stdout)
                    self.assertIn(f"Fix: Run {command_text}, then retry.", stdout)

    def test_every_admissible_surface_set_prepares_context_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            site_result = command.site.load_site(ROOT / "config/site.example.yaml")
            self.assertIsNotNone(site_result.config)
            assert site_result.config is not None
            for surfaces in _admissible_surfaces():
                with self.subTest(surfaces=surfaces):
                    host = EvalHost()
                    (code, stdout, stderr), contexts, target = _invoke_planted(
                        host, checkout, ranked_path, surfaces
                    )
                    self.assertEqual((code, stderr), (0, ""), stdout)
                    self.assertEqual(len(contexts), 1)
                    context = contexts[0]
                    self.assertEqual(
                        context.served_model_name is None,
                        CallSurface.ENGINE not in surfaces,
                    )
                    if CallSurface.ENGINE in surfaces:
                        self.assertEqual(context.served_model_name, target.served_model_name)
                    self.assertEqual(context.turns is None, CallSurface.TURNS not in surfaces)
                    self.assertEqual(context.image is None, CallSurface.IMAGE not in surfaces)
                    self.assertEqual(context.generation is None, CallSurface.STORES not in surfaces)
                    generation_reads = [
                        (argv, payload) for argv, payload in host.calls
                        if "FROM public.index_generations" in (payload or "")
                    ]
                    self.assertEqual(len(generation_reads), int(CallSurface.STORES in surfaces))
                    if CallSurface.STORES in surfaces:
                        row = _generation_row()
                        assert context.generation is not None
                        self.assertEqual(
                            (context.generation.name, context.generation.corpus_lockfile,
                             context.generation.state),
                            (row["gen_id"], row["corpus_lockfile"], row["state"]),
                        )
                    if CallSurface.IMAGE in surfaces:
                        assert context.image is not None
                        self.assertEqual(context.image.checkout, checkout)
                        self.assertIn("gideon@sha256:", context.image.reference)
                    self.assertEqual(
                        context.ranked is None, CallSurface.RANKED_FILE not in surfaces
                    )
                    sql = next(
                        input for argv, input in host.calls
                        if argv[0] == "docker" and input is not None and "INSERT INTO eval_runs" in input
                    )
                    assert sql is not None
                    profile_line = next(
                        line for line in sql.splitlines()
                        if line.startswith("\\set hardware_profile ")
                    )
                    self.assertEqual(
                        read_psql_set(profile_line, "hardware_profile"),
                        target.profile_name
                        if CallSurface.ENGINE in surfaces
                        else site_result.config.hardware_profile,
                    )

    def test_image_only_skips_engine_guards_while_smoke_keeps_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3})
            ranked_path, _ = _ranked_file(directory, {1: (1,)})
            surfaces = SLICE_RUNNERS["smoke"].surfaces
            self.assertTrue({CallSurface.ENGINE, CallSurface.IMAGE} <= surfaces)
            site_result = command.site.load_site(ROOT / "config/site.example.yaml")
            assert site_result.config is not None
            window_detail = command.window.window_judgement(
                NOW, site_result.config.office.timezone
            ).description
            host = EvalHost()
            original_exists = host.exists
            with patch.object(
                host, "exists",
                side_effect=lambda path: Path(path) == command.nogpu.NO_GPU_PATH or original_exists(path),
            ):
                (code, stdout, _), contexts, _ = _invoke_planted(
                    host, checkout, ranked_path, frozenset({CallSurface.IMAGE})
                )
                self.assertEqual(code, 0, stdout)
                self.assertEqual(len(contexts), 1)
                self.assertIsNotNone(contexts[0].image)
                self.assertNotIn(window_detail, stdout)
                self.assertNotIn("engine lock", stdout)
                self.assertEqual(host.lock_records, [])

                (code, stdout, _), contexts, _ = _invoke_planted(
                    EvalHost(), checkout, ranked_path, surfaces
                )
                self.assertEqual(code, 0, stdout)
                self.assertEqual(len(contexts), 1)
                self.assertIn("engine lock", stdout)
                self.assertIn(window_detail, stdout)

                (code, stdout, _), contexts, _ = _invoke_planted(
                    host, checkout, ranked_path, surfaces
                )
                self.assertEqual(code, 1)
                self.assertIn("no-GPU marker refuses", stdout)
                self.assertEqual(contexts, [])

    def test_image_refuses_unbuilt_pin_and_absent_daemon_image_with_fixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3})
            ranked_path, _ = _ranked_file(directory, {1: (1,)})
            loaded = command.images.load_image_lock(ROOT / "images.lock")
            assert loaded.lock is not None
            unbuilt = replace(loaded.lock, images=tuple(
                replace(pin, digest="unbuilt") if pin.name == "gideon" else pin
                for pin in loaded.lock.images
            ))
            with patch.object(command.images, "load_image_lock", return_value=replace(loaded, lock=unbuilt)):
                (code, stdout, _), contexts, _ = _invoke_planted(
                    EvalHost(), checkout, ranked_path, frozenset({CallSurface.IMAGE})
                )
            self.assertEqual(code, 1)
            self.assertEqual(contexts, [])
            self.assertIn("gideon image is unbuilt", stdout)
            self.assertIn("tools.imagebuild gideon", stdout)

            host = EvalHost()
            host.image_probe_rc = 1
            (code, stdout, _), contexts, _ = _invoke_planted(
                host, checkout, ranked_path, frozenset({CallSurface.IMAGE})
            )
            self.assertEqual(code, 1)
            self.assertEqual(contexts, [])
            self.assertIn("image is absent", stdout)
            self.assertIn("gideon registry mirror", stdout)
            self.assertEqual(host.calls[-1][0][:3], ("docker", "image", "inspect"))

    def test_database_refusal_depends_on_whether_a_surface_reaches_the_box(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            for surfaces in _admissible_surfaces():
                with self.subTest(surfaces=surfaces):
                    host = EvalHost(probe_rc=1)
                    (code, stdout, stderr), contexts, _target = _invoke_planted(
                        host, checkout, ranked_path, surfaces
                    )
                    self.assertEqual(stderr, "")
                    self.assertFalse(
                        any("INSERT INTO eval_runs" in (input or "") for _, input in host.calls)
                    )
                    if any(surface.on_box for surface in surfaces):
                        self.assertEqual(code, 1)
                        self.assertEqual(stdout.count("preconditions: refuse"), 1)
                        self.assertNotIn("run: ok", stdout)
                        self.assertEqual(contexts, [])
                    else:
                        self.assertEqual(code, 0)
                        self.assertNotIn("preconditions:", stdout)
                        self.assertIn("record: ok — skipped — no database reachable", stdout)
                        self.assertEqual(len(contexts), 1)
                        self.assertEqual([argv[0] for argv, _ in host.calls], ["docker"])

    def test_supplied_set_holds_skip_row_without_off_box_host_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            supplied_set = Path(directory) / (checkout / SET_ROOT).name
            shutil.copytree(checkout / SET_ROOT, supplied_set)
            for surfaces in _admissible_surfaces():
                with self.subTest(surfaces=surfaces):
                    host = EvalHost()
                    (code, stdout, stderr), contexts, _target = _invoke_planted(
                        host, checkout, ranked_path, surfaces, supplied_set=supplied_set
                    )
                    self.assertEqual((code, stderr), (0, ""), stdout)
                    self.assertEqual(len(contexts), 1)
                    self.assertIn(
                        "record: ok — skipped — a set outside the release is never recorded",
                        stdout,
                    )
                    self.assertFalse(
                        any("INSERT INTO eval_runs" in (input or "") for _, input in host.calls)
                    )
                    if not any(surface.on_box for surface in surfaces):
                        self.assertEqual(host.calls, [])

    def test_off_box_site_refusal_prints_after_run_and_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broken_site = Path(directory) / "broken-site.yaml"
            broken_site.write_text("broken: [\n", encoding="utf-8")
            host = EvalHost()
            kwargs = _run_kwargs(host)
            kwargs["site_path"] = broken_site
            off_box = dict(SLICE_RUNNERS)
            off_box["extraction"] = replace(off_box["extraction"], surfaces=frozenset())
            with patch.object(command, "SLICE_RUNNERS", off_box):
                code, stdout, stderr = _invoke(
                    ["eval", "run", "--slice", "extraction", "--stack", "production"],
                    **kwargs,
                )
        self.assertEqual((code, stderr), (1, ""))
        lines = stdout.splitlines()
        run_index = next(i for i, line in enumerate(lines) if line.startswith("run: ok"))
        verdict_index = next(i for i, line in enumerate(lines) if line.startswith("verdict "))
        record_index = next(i for i, line in enumerate(lines) if line.startswith("record: refuse"))
        gate_index = next(i for i, line in enumerate(lines) if line.startswith("gate:"))
        # The runner's report sits between the run row and the verdict line.
        self.assertLess(run_index + 1, verdict_index)
        self.assertLess(verdict_index, record_index)
        self.assertLess(record_index, gate_index)
        self.assertTrue(lines[record_index].startswith(
            "record: refuse — site file could not be loaded:"
        ))


class StoresPreparation(unittest.TestCase):
    """A readable generation is resolved before a stores runner and its record."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout, _ids = _fixture(temporary.name, {1: 3})
        self.ranked_path, _data = _ranked_file(temporary.name, {1: (1,)})
        self.surfaces = frozenset({CallSurface.STORES})

    def assert_refusal(
        self, host: EvalHost, outcome: tuple[int, str, str], contexts: list[RunContext],
        problem: str, fix: str,
    ) -> None:
        code, stdout, stderr = outcome
        self.assertEqual((code, stderr, contexts), (1, "", []), stdout)
        rows = [line for line in stdout.splitlines() if line.startswith("preconditions:")]
        self.assertEqual(len(rows), 1, stdout)
        self.assertIn(f"preconditions: refuse — {problem} Fix: ", rows[0])
        self.assertIn(fix, rows[0])
        self.assertLessEqual(rows[0].partition(" Fix: ")[2].count("--generation"), 1)
        self.assertNotIn("run: ok", stdout)
        self.assertNotIn("record:", stdout)
        self.assertNotIn("Traceback", stdout + stderr)
        self.assertFalse(any("INSERT INTO eval_runs" in (payload or "") for _argv, payload in host.calls))

    def test_invalid_name_and_missing_or_ambiguous_generation_refuse_with_selection_fix(self) -> None:
        row = _generation_row()
        other = {**row, "gen_id": row["gen_id"] + "-other"}
        cases = (
            ("not-a-generation", "", "not a generation name"),
            (row["gen_id"], "", f"generation {row['gen_id']} is not recorded on the {PRODUCTION_STACK} stack"),
            (None, "", f"no live {command.generation.COLLECTION} generation on the {PRODUCTION_STACK} stack"),
            (None, json.dumps(row) + "\n" + json.dumps(other) + "\n",
             f"more than one live {command.generation.COLLECTION} generation on the {PRODUCTION_STACK} stack"),
        )
        retry = report.command("eval run --slice judgments --kind smoke")
        for name, reply, problem in cases:
            with self.subTest(name=name, problem=problem):
                host = EvalHost()
                outcome, contexts, _ = _invoke_planted(
                    host, self.checkout, self.ranked_path, self.surfaces,
                    generation_name=name, generation_reply=reply, flags=("--kind", "smoke"),
                )
                self.assert_refusal(host, outcome, contexts, problem, f"{retry} --generation <name>")
                self.assertIn(report.command("index report"), outcome[1])
                self.assertFalse(any(argv[0] == "git" or payload == "SELECT 1;\n" for argv, payload in host.calls))
                if name == "not-a-generation":
                    self.assertEqual(host.calls, [])
                else:
                    self.assertEqual(len(host.calls), 1)
                    argv, sql = host.calls[0]
                    self.assertEqual(argv, tuple(command.record.reader_argv(_run_kwargs(host)["rendered_dir"])))
                    assert sql is not None
                    if name is None:
                        self.assertIn("collection = :'v_collection' AND state = 'live'", sql)
                        self.assertIn("ORDER BY gen_id", sql)
                    else:
                        self.assertIn("gen_id = :'v_name'", sql)

    def test_unreadable_states_name_the_build_and_preserve_retry_flags_once(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        for stack_name in (PRODUCTION_STACK, CI_STACK):
            for state in ("building", "failed", "retired"):
                for installed in (False, True):
                    with self.subTest(stack=stack_name, state=state, installed=installed):
                        environment = {report.GIDEON_INSTALLED_COMMAND: "/fictitious/gideon"} if installed else {}
                        row = _generation_row(state=state)
                        host = EvalHost()
                        with patch.dict(os.environ, environment, clear=True):
                            outcome, contexts, _ = _invoke_planted(
                                host, self.checkout, self.ranked_path, self.surfaces,
                                stack_name=stack_name, generation_name=row["gen_id"],
                                generation_reply=json.dumps(row) + "\n", flags=("--kind", "smoke"),
                            )
                            flags = f" --stack {CI_STACK}" if stack_name == CI_STACK else ""
                            retry = report.command(f"eval run --slice judgments{flags} --kind smoke")
                            build = report.command(
                                f"index build {command.generation.COLLECTION}"
                                + (" --sample" if stack_name == CI_STACK else "")
                            )
                        self.assert_refusal(
                            host, outcome, contexts, f"generation {row['gen_id']} is {state}",
                            f"Finish its build ({build})",
                        )
                        self.assertIn(f"{retry} --generation <name>", outcome[1])

    def test_failed_or_unavailable_read_refuses_even_with_a_valid_reply(self) -> None:
        for code, error, problem in (
            (1, None, "generation record read failed (exit 1)"),
            (127, None, "generation record read failed (exit 127)"),
            (0, OSError("fictitious unavailable tool"), "generation record read could not run"),
            (0, subprocess.TimeoutExpired("fictitious psql", 1), "generation record read could not run"),
        ):
            with self.subTest(code=code, error=error):
                host = EvalHost()
                host.generation_code, host.generation_error = code, error
                outcome, contexts, _ = _invoke_planted(
                    host, self.checkout, self.ranked_path, self.surfaces,
                    generation_name=_generation_row()["gen_id"],
                )
                self.assert_refusal(
                    host, outcome, contexts, problem,
                    command.stack.logs_fix(_run_kwargs(host)["rendered_dir"], command.record.POSTGRES_SERVICE),
                )
                self.assertIn(report.command("eval run --slice judgments"), outcome[1])
                self.assertEqual(len(host.calls), 1)

    def test_invalid_record_rows_refuse_without_diagnostics_or_tracebacks(self) -> None:
        row = _generation_row()
        invalid_rows: tuple[object, ...] = (
            [], {}, {**row, "unexpected": "fictitious private diagnostic"},
            {key: value for key, value in row.items() if key != "state"},
            {**row, "gen_id": "not-a-generation"},
            {**row, "gen_id": row["gen_id"] + "-different"},
            {**row, "corpus_lockfile": "not-a-lockfile"}, {**row, "state": "unknown"},
            {**row, "gen_id": None}, {**row, "corpus_lockfile": 123}, {**row, "state": []},
        )
        replies = ("fictitious malformed JSON\n", *(json.dumps(value) + "\n" for value in invalid_rows),
                   json.dumps(row) + "\n" + json.dumps(row) + "\n")
        for reply in replies:
            with self.subTest(reply=reply):
                host = EvalHost()
                outcome, contexts, _ = _invoke_planted(
                    host, self.checkout, self.ranked_path, self.surfaces,
                    generation_name=row["gen_id"], generation_reply=reply,
                )
                self.assert_refusal(
                    host, outcome, contexts, "generation record row is invalid",
                    command.stack.logs_fix(_run_kwargs(host)["rendered_dir"], command.record.POSTGRES_SERVICE),
                )
                self.assertNotIn("fictitious private diagnostic", outcome[1] + outcome[2])
                self.assertNotIn("fictitious generation diagnostic", outcome[1] + outcome[2])
                self.assertEqual(len(host.calls), 1)

    def test_stores_reads_selected_stack_after_sibling_job_and_before_record_facts(self) -> None:
        row = _generation_row(state="built")
        for stack_name in (CI_STACK, PRODUCTION_STACK):
            for surfaces in (self.surfaces, self.surfaces | {CallSurface.ENGINE}):
                with self.subTest(stack=stack_name, surfaces=surfaces):
                    host = EvalHost()
                    (code, stdout, stderr), contexts, _ = _invoke_planted(
                        host, self.checkout, self.ranked_path, surfaces, stack_name=stack_name,
                        generation_name=row["gen_id"], generation_reply=json.dumps(row) + "\n",
                    )
                    self.assertEqual((code, stderr), (0, ""), stdout)
                    self.assertEqual(len(contexts), 1)
                    context = contexts[0]
                    self.assertIsNotNone(context.generation)
                    self.assertEqual(stdout.count("preconditions:"), 1)
                    self.assertIn(f", generation {row['gen_id']} ({row['corpus_lockfile']}, {row['state']})", stdout)
                    read_index = next(i for i, (_argv, sql) in enumerate(host.calls) if "index_generations" in (sql or ""))
                    argv, sql = host.calls[read_index]
                    self.assertEqual(argv, tuple(command.record.reader_argv(context.turns_dir)))
                    self.assertNotIn(row["gen_id"], " ".join(argv))
                    assert sql is not None
                    line = next(line for line in sql.splitlines() if line.startswith("\\set v_name "))
                    self.assertEqual(read_psql_set(line, "v_name"), row["gen_id"])
                    jobs = [i for i, (_argv, sql) in enumerate(host.calls) if "procrastinate_jobs" in (sql or "")]
                    if stack_name == CI_STACK:
                        self.assertEqual(len(jobs), 1)
                        self.assertLess(jobs[0], read_index)
                        self.assertIn(str(context.turns_dir), host.calls[jobs[0]][0])
                        self.assertEqual(Path(context.turns_dir), Path(command.stacks.CI_ROOT))
                    else:
                        self.assertEqual(jobs, [])
                        self.assertEqual(context.turns_dir, Path(context.production_dir))
                    facts = [
                        i for i, (argv, payload) in enumerate(host.calls)
                        if argv[0] == "git" or payload == "SELECT 1;\n"
                    ]
                    self.assertTrue(facts)
                    self.assertTrue(all(read_index < i for i in facts))
                    write_argv = next(argv for argv, sql in host.calls if "INSERT INTO eval_runs" in (sql or ""))
                    self.assertIn(str(context.production_dir), write_argv)
                    if CallSurface.ENGINE not in surfaces:
                        self.assertEqual(host.lock_records, [])
                        self.assertNotIn("engine lock", stdout)

    def test_live_sibling_job_refuses_before_generation_or_record_reads(self) -> None:
        host = EvalHost()
        host.queue_output = "73\n"
        outcome, contexts, _ = _invoke_planted(
            host, self.checkout, self.ranked_path, self.surfaces, stack_name=CI_STACK,
            generation_name=_generation_row()["gen_id"],
        )
        self.assert_refusal(
            host, outcome, contexts, "a sample build is running on the sibling (job 73)",
            report.command("index build caselaw --sample"),
        )
        self.assertEqual(len(host.calls), 1)
        self.assertIn("procrastinate_jobs", host.calls[0][1] or "")
        self.assertEqual(host.lock_records, [])

    def test_record_facts_still_refuse_after_generation_before_the_runner(self) -> None:
        for surfaces in (self.surfaces, self.surfaces | {CallSurface.ENGINE}):
            for failure in ("provenance", "writer"):
                with self.subTest(surfaces=surfaces, failure=failure):
                    host = EvalHost(commit_rc=1) if failure == "provenance" else EvalHost(probe_rc=1)
                    outcome, contexts, _ = _invoke_planted(
                        host, self.checkout, self.ranked_path, surfaces,
                    )
                    self.assert_refusal(
                        host, outcome, contexts,
                        "git provenance could not be read" if failure == "provenance"
                        else "eval writer failed: exit 1",
                        "as the checkout owner" if failure == "provenance" else "logs postgres",
                    )
                    self.assertIn("index_generations", host.calls[0][1] or "")
                    self.assertTrue(any(argv[0] == "git" for argv, _payload in host.calls))


class SiblingSurfaceRule(unittest.TestCase):
    """The CI sibling admits slices that name a surface on the box."""

    def test_load_refuses_no_box_surface_even_with_a_judge_prompt(self) -> None:
        replacement = dict(SLICE_RUNNERS)
        replacement["extraction"] = replace(
            replacement["extraction"],
            surfaces=frozenset(),
            judge_prompt="fictitious-prompt",
        )
        with (
            patch.object(command, "SLICE_RUNNERS", replacement),
            patch.object(command.stacks.secrets, "select_directory") as select_directory,
        ):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction", "--stack", "ci"],
                **_run_kwargs(EvalHost()),
            )
            select_directory.assert_called_once()
        self.assertEqual((code, stderr), (1, ""))
        self.assertIn(
            "load: refuse — slice extraction cannot run on the CI sibling: "
            "the slice reaches nothing a stack names "
            "Fix: Run this slice with --stack production, then retry.",
            stdout,
        )
        self.assertNotIn("load: ok", stdout)
        self.assertNotIn("preconditions:", stdout)

    def test_load_admits_engine_only_without_a_judge_prompt(self) -> None:
        replacement = dict(SLICE_RUNNERS)
        replacement["extraction"] = replace(
            replacement["extraction"],
            surfaces=frozenset({CallSurface.ENGINE}),
            judge_prompt=None,
        )
        with (
            patch.object(command, "SLICE_RUNNERS", replacement),
            patch.object(command.stacks.secrets, "select_directory") as select_directory,
        ):
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "extraction", "--stack", "ci"],
                **_run_kwargs(EvalHost(effective_uid=1)),
            )
            select_directory.assert_called_once()
        self.assertEqual((code, stderr), (1, ""))
        self.assertIn("load: ok", stdout)
        self.assertNotIn("load: refuse", stdout)
        self.assertIn("preconditions: refuse — root privileges are required", stdout)


class PreparationBoundaries(unittest.TestCase):
    """Registry surfaces and prepared record facts stay within their boundaries."""

    def test_derived_flags_have_no_product_or_tool_readers(self) -> None:
        forbidden = {"reaches_engine", "drives_turns", "takes_ranked"}
        readers: list[str] = []
        for home in (ROOT / "gideon", ROOT / "tools"):
            for path in home.rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                readers.extend(
                    f"{path.relative_to(ROOT)}:{node.lineno}: {node.attr}"
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Load)
                    and node.attr in forbidden
                )
        self.assertEqual(readers, [])

    def test_record_stage_does_not_resolve_site_provenance_or_writer(self) -> None:
        path = Path(inspect.getfile(command))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        record_function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_record"
        )
        forbidden = {"load_site", "_provenance", "probe"}
        calls = []
        for node in ast.walk(record_function):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else (
                function.attr if isinstance(function, ast.Attribute) else None
            )
            if name in forbidden:
                calls.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
        self.assertEqual(calls, [])
