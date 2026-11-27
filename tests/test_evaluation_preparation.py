"""Contracts for the evaluation slice registry's call surfaces."""

import ast
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import yaml  # type: ignore[import-untyped]
from test_engine_verify import completed, models_text
from test_evaluation_assessments import FIXTURE_MODEL, FIXTURE_PROMPT
from test_evaluation_record import read_psql_set
from test_evaluation_run import (
    NOW,
    ROOT,
    EvalHost,
    _copy_sibling_artifacts,
    _generation_row,
    _invoke,
    _run_kwargs,
    _sibling_row,
    fixture_judge_retry,
)
from test_judgments_slice import (
    _assessed_file,
    _coordinate,
    _fixture,
    _judgments_host,
    _ranked_file,
)

from gideon.evaluation import baselines, command, sampling
from gideon.evaluation.assessments import (
    ASSESSMENTS_FIX,
    ASSESSMENTS_LABEL,
    Assessments,
)
from gideon.evaluation.evalset import SET_ROOT, LoadedSet
from gideon.evaluation.rankmetrics import RELEVANT_GRADE
from gideon.evaluation.results import RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS, CallSurface
from gideon.host import backuplock, models, report, secrets, stack, weights
from gideon.host.corpus.lockfile import (
    read_lockfile_directory,
    render_errors,
    render_lockfile,
)
from gideon.host.render.ci import CI_STACK, PRODUCTION_STACK
from gideon.host.render.engine import GENERATOR, RERANK
from gideon.host.sysio import ReadBytesHost

_ENGINE_RESEARCH_SURFACES = frozenset({CallSurface.ENGINE, CallSurface.RESEARCH, CallSurface.STORES})


class SliceSurfaces(unittest.TestCase):
    """Each registry entry names the surfaces its runs reach."""

    def test_every_entry_names_its_surfaces_from_the_closed_list(self) -> None:
        for name, spec in SLICE_RUNNERS.items():
            with self.subTest(slice_name=name):
                self.assertIs(type(spec.surfaces), frozenset)
                self.assertTrue(all(type(surface) is CallSurface for surface in spec.surfaces))

    def test_every_entry_names_an_on_box_surface(self) -> None:
        on_box = {surface for surface in CallSurface if surface.on_box}
        self.assertEqual(on_box, {
            CallSurface.ENGINE, CallSurface.IMAGE, CallSurface.RECORD,
            CallSurface.RESEARCH, CallSurface.STORES, CallSurface.TURNS,
        })
        for name, spec in SLICE_RUNNERS.items():
            with self.subTest(slice_name=name):
                self.assertTrue(spec.surfaces & on_box)

    def test_construction_requires_an_on_box_surface(self) -> None:
        spec = SLICE_RUNNERS["extraction"]
        for surfaces in (frozenset(), frozenset({CallSurface.RANKED_FILE})):
            with (
                self.subTest(surfaces=surfaces),
                self.assertRaisesRegex(ValueError, "must name at least one surface on the box"),
            ):
                replace(spec, surfaces=surfaces)
        surfaces = frozenset({CallSurface.RESEARCH})
        self.assertEqual(replace(spec, surfaces=surfaces).surfaces, surfaces)

    def test_turns_without_the_engine_is_refused_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "turns surface must name the engine surface"):
            replace(SLICE_RUNNERS["extraction"], surfaces=frozenset({CallSurface.TURNS}))

    def test_accepting_a_ranked_file_without_research_is_refused_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "accepting a ranked file must name the Research surface"):
            replace(SLICE_RUNNERS["extraction"], accepts_ranked=True)

    def test_accepting_selection_without_research_is_refused_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "accepting --unsigned and --tuning must name the Research surface"):
            replace(SLICE_RUNNERS["extraction"], accepts_selection=True)

    def test_replacing_a_runner_keeps_the_surfaces(self) -> None:
        for name, original in SLICE_RUNNERS.items():
            with self.subTest(slice_name=name):
                runner = SLICE_RUNNERS["extraction"].runner
                changed = replace(original, runner=runner)
                self.assertIs(changed.runner, runner)
                self.assertEqual(changed.surfaces, original.surfaces)


def _admissible_surfaces() -> tuple[tuple[frozenset[CallSurface], bool], ...]:
    """Every admissible set of declared surfaces, with and without the ranked door."""

    admissible = []
    declared = tuple(surface for surface in CallSurface if surface is not CallSurface.RANKED_FILE)
    for size in range(len(declared) + 1):
        for chosen in combinations(declared, size):
            surfaces = frozenset(chosen)
            for use_ranked in (False, True):
                try:
                    replace(SLICE_RUNNERS["judgments"], surfaces=surfaces, accepts_ranked=use_ranked)
                except ValueError:
                    continue
                admissible.append((surfaces, use_ranked))
    return tuple(admissible)


def _invoke_planted(
    host: EvalHost,
    checkout: Path,
    ranked_path: Path,
    surfaces: frozenset[CallSurface],
    *,
    use_ranked: bool = False,
    supplied_set: Path | None = None,
    site_path: Path | None = None,
    stack_name: str = PRODUCTION_STACK,
    generation_name: str | None = None,
    generation_reply: str | None = None,
    flags: tuple[str, ...] = (),
    research_probe: Mock | None = None,
) -> tuple[tuple[int, str, str], list[RunContext], command.engine.EngineTarget]:
    """Run one surface set through the command with observable seams."""

    contexts: list[RunContext] = []
    row = (
        _sibling_row(checkout, site_path or ROOT / "config/site.example.yaml")
        if stack_name == CI_STACK and generation_name is None
        and CallSurface.STORES in surfaces and generation_reply is None
        else _generation_row()
    )
    host.generation_output = (
        json.dumps(row) + "\n"
        if generation_reply is None else generation_reply
    )

    def run_slice(_loaded: LoadedSet, _name: str, context: RunContext) -> SliceResult:
        contexts.append(context)
        return SliceResult(True, "visibly fictitious runner report\n", ())

    target = command.engine.EngineTarget("fictitious-target-profile", "fictitious-model", 1)
    replacement = dict(SLICE_RUNNERS)
    replacement["judgments"] = replace(
        replacement["judgments"], surfaces=surfaces, runner=run_slice, accepts_ranked=use_ranked,
    )
    argv = ["eval", "run", "--slice", "judgments", "--stack", stack_name, *flags]
    if generation_name is not None:
        argv.extend(("--generation", generation_name))
    if use_ranked:
        argv.extend(("--ranked", str(ranked_path)))
    if supplied_set is not None:
        argv.extend(("--set", str(supplied_set)))
    kwargs = _run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml")
    kwargs["models_path"] = ROOT / "models.lock"
    kwargs["clock"] = lambda: NOW
    if site_path is not None:
        kwargs["site_path"] = site_path
    if research_probe is None:
        research_probe = Mock(return_value=None)
    with (
        patch.object(command, "SLICE_RUNNERS", replacement),
        patch.object(command.engine, "resolve_engine_target", return_value=target),
        patch.object(command.sampling, "hold_running") as hold,
        patch.object(command.access, "read_eval_password", return_value="fictitious-password"),
        patch.object(
            command.access, "make_client_factory", return_value=lambda: object()
        ),
        patch.object(
            command.door,
            "probe",
            return_value=command.door.ProbeResult(True, "door ready", None),
        ),
        patch.object(command.research_door, "probe", new=research_probe),
        patch.object(command.stacks.secrets, "select_directory") as select_directory,
    ):
        outcome = _invoke(argv, **kwargs)
        hold.assert_not_called()
        if stack_name == CI_STACK:
            select_directory.assert_called_once()
        else:
            select_directory.assert_not_called()
    return outcome, contexts, target


class PreparationRoutes(unittest.TestCase):
    """Each named surface supplies its runner context and record facts."""

    def test_judge_hold_uses_injected_paths_under_the_lock_and_refuses_a_different_pin(self) -> None:
        lock_text = models_text()
        lock = models.load_models_lock_text(lock_text).lock
        assert lock is not None
        profile = lock.profile(lock.reference)
        assert profile is not None
        pin = profile.model(GENERATOR.role)
        assert pin is not None
        slice_name = "judge-triples"
        spec = SLICE_RUNNERS[slice_name]
        target = command.engine.EngineTarget(profile.name, pin.serve.served_name, 1000)
        rendered = Path("/tmp/fictitious-judge-production")
        container = "fictitious-judge-container"
        locate = stack.compose_argv(rendered, "ps", "--status", "running", "--quiet", GENERATOR.service_name)
        inspect_command = ["docker", "container", "inspect", "--format", "{{json .Config.Cmd}}", "--", container]
        other_digit = "f" if pin.revision[0] != "f" else "e"
        with tempfile.TemporaryDirectory() as directory:
            models_path = Path(directory) / "fictitious-models.lock"
            models_path.write_text(lock_text, encoding="utf-8")
            models_root = Path(directory) / "fictitious-weights"
            config = weights.snapshot_path(pin.repo, pin.revision, "generation_config.json", root=models_root)
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps(dict(sampling.THINKING_SAMPLING)), encoding="utf-8")

            for revision in (pin.revision, other_digit + pin.revision[1:]):
                with self.subTest(matched=revision == pin.revision):
                    host = EvalHost()

                    def pin_reply(
                        argv: list[str], *, host: EvalHost = host, revision: str = revision,
                    ) -> subprocess.CompletedProcess[str]:
                        self.assertIn(backuplock.ENGINE_LOCK.path, host.locks)
                        if argv == locate:
                            return completed(argv, stdout=container)
                        self.assertEqual(argv, inspect_command)
                        return completed(argv, stdout=json.dumps([pin.repo, "--revision", revision]))

                    with (
                        patch.object(command.engine, "resolve_engine_target", return_value=target),
                        patch.object(host, "run", side_effect=pin_reply),
                        patch.object(host, "read_text", wraps=host.read_text) as read,
                        patch.object(command, "_run_repeats", return_value=command._RunRepeats(
                            SliceResult(True, "Fictitious judge report.", ()), 1, 1, None,
                        )) as runner,
                    ):
                        kwargs = _run_kwargs(host)
                        kwargs.update(rendered_dir=rendered, models_path=models_path, models_root=models_root)
                        code, stdout, stderr = _invoke(
                            ["eval", "run", "--slice", slice_name, "--set", str(ROOT / SET_ROOT), "--force"],
                            **kwargs,
                        )
                    self.assertEqual(stderr, "", stdout)
                    self.assertEqual(host.locks, {})
                    rows = [line for line in stdout.splitlines() if line.startswith("preconditions:")]
                    row, = rows
                    if revision == pin.revision:
                        self.assertEqual(code, 0, stdout)
                        self.assertTrue(row.startswith("preconditions: ok"), row)
                        self.assertIn(f"prompt {spec.judge_prompt}, judge retry seed {sampling.DEFAULT_SEED},", row)
                        runner.assert_called_once()
                        context = runner.call_args.args[3]
                        self.assertEqual(context.judge_retry.seed, sampling.DEFAULT_SEED)
                        read.assert_any_call(config)
                    else:
                        self.assertEqual(code, 1, stdout)
                        self.assertIn("preconditions: refuse — the running generator repository or revision differs", row)
                        self.assertIn(f"Run {report.command('apply')} from the checkout holding the intended model pin", stdout)
                        runner.assert_not_called()
                        self.assertNotIn(config, [call.args[0] for call in read.call_args_list])

    def test_assessments_reach_the_runner_after_preconditions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, (case_id,) = _fixture(directory, {1: RELEVANT_GRADE})
            ranked_path, _ = _ranked_file(directory, {1: (1,)})
            coordinate = _coordinate(1)
            assessed_path, _ = _assessed_file(directory, ((case_id, coordinate, 0),))
            (code, stdout, stderr), contexts, _ = _invoke_planted(
                EvalHost(), checkout, ranked_path, SLICE_RUNNERS["judgments"].surfaces,
                use_ranked=True,
                flags=("--assessed", str(assessed_path)),
            )
        self.assertEqual((code, stderr), (0, ""), stdout)
        context, = contexts
        self.assertIsNone(context.judge_retry)
        self.assertEqual(context.ranked, {case_id: (coordinate,)})
        self.assertEqual(context.assessments, Assessments(
            {case_id: {coordinate: 0}}, FIXTURE_PROMPT, FIXTURE_MODEL,
        ))
        stages = [line.split(":", 1)[0] for line in stdout.splitlines() if ": ok" in line]
        self.assertEqual(stages, ["load", "ranked", "assessed", "preconditions", "run", "record", "gate"])

    def test_refused_assessments_stop_before_any_box_preconditions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ = _fixture(directory, {1: RELEVANT_GRADE})
            ranked_path, _ = _ranked_file(directory, {1: (1,)})
            assessed_path = Path(directory) / "fictional-matter-assessments.jsonl"
            sentinel = "Fictitious passage from an unreadable assessment"
            assessed_path.write_text(sentinel + "\n", encoding="utf-8")
            host = EvalHost()
            with patch.object(host, "geteuid", wraps=host.geteuid) as root_lookup:
                (code, stdout, stderr), contexts, _ = _invoke_planted(
                    host, checkout, ranked_path, SLICE_RUNNERS["judgments"].surfaces,
                    use_ranked=True,
                    flags=("--assessed", str(assessed_path)),
                )
                root_lookup.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(contexts, [])
        self.assertEqual(host.calls, [])
        self.assertEqual(host.lock_records, [])
        self.assertEqual([line.split(":", 1)[0] for line in stdout.splitlines()], [
            "load", "ranked", "assessed",
        ])
        self.assertIn(f"assessed: refuse — assessments file refused Fix: {ASSESSMENTS_FIX}", stdout)
        self.assertIn(f"{ASSESSMENTS_LABEL}:line 1: line is not one JSON object", stderr)
        self.assertNotIn(sentinel, stdout + stderr)
        self.assertNotIn(str(assessed_path), stdout + stderr)

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
            lock = models.load_models_lock(ROOT / "models.lock").lock
            assert lock is not None
            profile = lock.profile(site_result.config.hardware_profile)
            assert profile is not None
            reranker = profile.model(RERANK.role) is not None
            shipped = ((surfaces, use_ranked, None) for surfaces, use_ranked in _admissible_surfaces())
            for surfaces, use_ranked, baseline in (*shipped, (_ENGINE_RESEARCH_SURFACES, False, "off")):
                with self.subTest(surfaces=surfaces, use_ranked=use_ranked, baseline=baseline):
                    host = EvalHost()
                    research_probe = Mock(return_value=None)
                    flags = () if baseline is None else ("--baseline", baseline)
                    (code, stdout, stderr), contexts, target = _invoke_planted(
                        host, checkout, ranked_path, surfaces, use_ranked=use_ranked, flags=flags,
                        research_probe=research_probe,
                    )
                    self.assertEqual((code, stderr), (0, ""), stdout)
                    self.assertEqual(len(contexts), 1)
                    context = contexts[0]
                    turn_overrides = None if baseline is None else baselines.BASELINES[baseline]
                    self.assertEqual(context.turn_overrides, turn_overrides)
                    has_research = CallSurface.RESEARCH in surfaces and not use_ranked
                    if has_research:
                        research_probe.assert_called_once_with(host, context.turns_dir, sibling=False)
                        expected_hash = baselines.expected_config_hash(turn_overrides, reranker=reranker)
                        self.assertIsInstance(expected_hash, str)
                        self.assertEqual(context.expected_config_hash, expected_hash)
                    else:
                        research_probe.assert_not_called()
                        self.assertIsNone(context.expected_config_hash)
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
                        if use_ranked:
                            self.assertLess(stdout.index("ranked: ok"), stdout.index("preconditions: ok"))
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
                        context.ranked is not None, use_ranked,
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
                    if has_research:
                        overrides_line = next(line for line in sql.splitlines() if line.startswith("\\set overrides "))
                        facts: dict[str, object] = {
                            "door": "turn", "config_hash": context.expected_config_hash,
                        }
                        if turn_overrides is not None:
                            facts.update(baseline=baseline, turn_overrides=dict(turn_overrides))
                        if CallSurface.ENGINE in surfaces:
                            self.assertEqual(json.loads(read_psql_set(overrides_line, "overrides")), {"research": facts})
                        else:
                            self.assertEqual(json.loads(read_psql_set(overrides_line, "overrides"))["judgments"]["config_hash"],
                                             context.expected_config_hash)
                        preconditions, = (line for line in stdout.splitlines() if line.startswith("preconditions:"))
                        self.assertTrue(preconditions.endswith(
                            f", door {command.research_door.TURN_PATH} probed, baseline {baseline or 'shipped'}"
                        ), preconditions)

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

    def test_unreachable_database_refuses_during_preconditions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            for surfaces, use_ranked in _admissible_surfaces():
                with self.subTest(surfaces=surfaces, use_ranked=use_ranked):
                    host = EvalHost(probe_rc=1)
                    (code, stdout, stderr), contexts, _target = _invoke_planted(
                        host, checkout, ranked_path, surfaces, use_ranked=use_ranked,
                    )
                    self.assertEqual((code, stderr), (1, ""), stdout)
                    self.assertEqual(stdout.count("preconditions: refuse"), 1)
                    self.assertNotIn("run: ok", stdout)
                    self.assertEqual(contexts, [])
                    self.assertFalse(
                        any("INSERT INTO eval_runs" in (input or "") for _, input in host.calls)
                    )

    def test_supplied_set_prints_skip_row_without_inserting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ids = _fixture(directory, {1: 3})
            ranked_path, _ranked_bytes = _ranked_file(directory, {1: (1,)})
            supplied_set = Path(directory) / (checkout / SET_ROOT).name
            shutil.copytree(checkout / SET_ROOT, supplied_set)
            for surfaces, use_ranked in _admissible_surfaces():
                with self.subTest(surfaces=surfaces, use_ranked=use_ranked):
                    host = EvalHost()
                    (code, stdout, stderr), contexts, _target = _invoke_planted(
                        host, checkout, ranked_path, surfaces,
                        use_ranked=use_ranked, supplied_set=supplied_set,
                    )
                    self.assertEqual((code, stderr), (0, ""), stdout)
                    self.assertEqual(len(contexts), 1)
                    self.assertIn(
                        "record: ok — skipped — a set outside the release is never recorded",
                        stdout.splitlines(),
                    )
                    self.assertFalse(
                        any("INSERT INTO eval_runs" in (input or "") for _, input in host.calls)
                    )


class StoresPreparation(unittest.TestCase):
    """A readable generation is resolved before a stores runner and its record."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout, _ids = _fixture(temporary.name, {1: 3})
        _copy_sibling_artifacts(self.checkout)
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

    def test_unnamed_ci_uses_newest_committed_label_and_site_embedding_space(self) -> None:
        """A cut selects its own sample, including an instrument-only cut."""

        directory = self.checkout / "corpus/lockfiles"
        loaded = read_lockfile_directory(directory)
        latest = loaded.newest_on(None)
        assert latest is not None
        label = f"corpus-{date.fromisoformat(latest.label.removeprefix('corpus-')) + timedelta(days=1)}"
        shutil.copytree(directory / latest.label, directory / label)
        (directory / f"{label}.yaml").write_text(render_lockfile(replace(
            latest, label=label, base=latest.label, reason="instrument",
        )))
        # A companion directory without its YAML is an unfinished cut, not the default.
        (directory / "corpus-2999-01-01").mkdir()
        path = self.checkout / "models.lock"
        document = yaml.safe_load(path.read_text())
        configured = command.site.load_site(ROOT / "config/site.example.yaml")
        assert configured.config is not None
        document["profiles"][configured.config.hardware_profile]["embedding_space"]["id"] = "fictitious-other-space"
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        expected = _sibling_row(self.checkout)
        self.assertEqual(expected["corpus_lockfile"], label)
        self.assertIn("fictitious-other-space", expected["gen_id"])
        host = EvalHost()
        (code, stdout, stderr), contexts, _ = _invoke_planted(
            host, self.checkout, self.ranked_path, self.surfaces,
            stack_name=CI_STACK, flags=("--kind", "smoke"),
        )
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(len(contexts), 1)
        resolved = contexts[0].generation
        assert resolved is not None
        self.assertEqual((resolved.name, resolved.corpus_lockfile, resolved.state), (
            expected["gen_id"], label, expected["state"],
        ))
        self.assertIn(f", generation {resolved.name} ({label}, {resolved.state})", stdout)
        reads = [sql for _argv, sql in host.calls if "FROM public.index_generations" in (sql or "")]
        self.assertEqual(len(reads), 1)
        assert reads[0] is not None
        self.assertIn("gen_id = :'v_name'", reads[0])
        self.assertNotIn("state = 'live'", reads[0])
        line = next(line for line in reads[0].splitlines() if line.startswith("\\set v_name "))
        self.assertEqual(read_psql_set(line, "v_name"), resolved.name)

    def test_unnamed_ci_uses_profiles_line_despite_newer_other_line(self) -> None:
        directory = self.checkout / "corpus/lockfiles"
        loaded = read_lockfile_directory(directory)
        original = loaded.newest_on(None)
        assert original is not None
        # After every committed lockfile on any line, so the copy is its line's newest.
        newest_date = max(
            date.fromisoformat(path.stem.removeprefix("corpus-"))
            for path in directory.glob("corpus-*.yaml")
        )
        selected = replace(original, label=f"corpus-{newest_date + timedelta(days=1)}", line="scotus", base=None)
        other = replace(original, label=f"corpus-{newest_date + timedelta(days=2)}", base=None)
        for lockfile in (selected, other):
            shutil.copytree(directory / original.label, directory / lockfile.label)
            (directory / f"{lockfile.label}.yaml").write_text(render_lockfile(lockfile))
        path = self.checkout / "models.lock"
        document = yaml.safe_load(path.read_text())
        configured = command.site.load_site(ROOT / "config/site.example.yaml")
        assert configured.config is not None
        document["profiles"][configured.config.hardware_profile]["corpus_line"] = selected.line
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        expected = _sibling_row(self.checkout)
        self.assertEqual(expected["corpus_lockfile"], selected.label)
        host = EvalHost()
        (code, out, err), contexts, _ = _invoke_planted(
            host, self.checkout, self.ranked_path, self.surfaces,
            stack_name=CI_STACK, generation_reply=json.dumps(expected),
        )
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(len(contexts), 1)
        resolved = contexts[0].generation
        assert resolved is not None
        self.assertEqual(resolved.name, expected["gen_id"])
        self.assertEqual(resolved.corpus_lockfile, selected.label)
        self.assertNotIn(other.label, out)

    def test_no_completed_lockfile_refuses_before_generation_or_runner(self) -> None:
        directory = self.checkout / "corpus/lockfiles"
        shutil.rmtree(directory)
        for unfinished in (False, True):
            with self.subTest(unfinished=unfinished):
                if unfinished:
                    (directory / "corpus-2999-01-01").mkdir(parents=True)
                host = EvalHost()
                outcome, contexts, _ = _invoke_planted(
                    host, self.checkout, self.ranked_path, self.surfaces,
                    stack_name=CI_STACK, generation_reply="",
                )
                self.assert_refusal(host, outcome, contexts, f"no completed corpus lockfile on line 'default' in {directory}", report.command("corpus cut"))
                self.assertFalse(any("index_generations" in (sql or "") for _argv, sql in host.calls))

    def test_malformed_or_unreadable_lockfiles_refuse_with_loader_text_and_restore_fix(self) -> None:
        directory = self.checkout / "corpus/lockfiles"
        path = next(directory.glob("*.yaml"))
        original = path.read_bytes()
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                path.write_bytes(original if unreadable else b"[]\n")
                host = EvalHost()
                with (
                    patch.object(host, "read_bytes", side_effect=PermissionError("fictitious unreadable lockfile"))
                    if unreadable else nullcontext()
                ):
                    errors = read_lockfile_directory(directory, host=cast(ReadBytesHost, host)).errors
                    self.assertTrue(errors)
                    outcome, contexts, _ = _invoke_planted(
                        host, self.checkout, self.ranked_path, self.surfaces,
                        stack_name=CI_STACK, generation_reply="",
                    )
                self.assert_refusal(host, outcome, contexts, render_errors(errors).splitlines()[0].split(" Fix: ")[0],
                                    "Restore the lockfile and its companion files from the release checkout.")
                self.assertFalse(any("index_generations" in (sql or "") for _argv, sql in host.calls))

    def test_production_default_and_explicit_ci_name_need_no_default_artifacts(self) -> None:
        shutil.rmtree(self.checkout / "corpus")
        (self.checkout / "models.lock").unlink()
        for stack_name, name in ((PRODUCTION_STACK, None), (CI_STACK, _generation_row()["gen_id"])):
            with self.subTest(stack=stack_name):
                host = EvalHost()
                (code, stdout, stderr), contexts, _ = _invoke_planted(
                    host, self.checkout, self.ranked_path, self.surfaces,
                    stack_name=stack_name, generation_name=name,
                )
                self.assertEqual((code, stderr), (0, ""), stdout)
                self.assertEqual(len(contexts), 1)
                sql = next(sql for _argv, sql in host.calls if "FROM public.index_generations" in (sql or ""))
                assert sql is not None
                self.assertIn("collection = :'v_collection' AND state = 'live'" if name is None else "gen_id = :'v_name'", sql)

    def test_default_refuses_model_lock_errors_and_names_an_unusable_space(self) -> None:
        path = self.checkout / "models.lock"
        original = path.read_text()
        configured = command.site.load_site(ROOT / "config/site.example.yaml")
        assert configured.config is not None
        document = yaml.safe_load(original)
        document["profiles"][configured.config.hardware_profile]["embedding_space"]["id"] = "x" * 256
        for text in ("[]\n", yaml.safe_dump(document, sort_keys=False)):
            with self.subTest(text=text):
                path.write_text(text)
                host = EvalHost()
                outcome, contexts, _ = _invoke_planted(
                    host, self.checkout, self.ranked_path, self.surfaces,
                    stack_name=CI_STACK, generation_reply="",
                )
                self.assertEqual((outcome[0], outcome[2], contexts), (1, "", []), outcome[1])
                self.assertIn("models.lock", outcome[1])
                self.assertIn("Fix:", outcome[1])
                self.assertIn(report.command("eval run --slice judgments --stack ci"), outcome[1])
                self.assertFalse(any("index_generations" in (sql or "") for _argv, sql in host.calls))

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

    def test_judgments_prepares_ranked_file_before_resolving_the_sibling_default(self) -> None:
        self.addCleanup(secrets.select_directory, secrets.current_directory())
        with tempfile.TemporaryDirectory() as directory:
            checkout, _ = _fixture(directory, {1: 2})
            ranked_path, _ = _ranked_file(directory, {1: (1,)})
            _copy_sibling_artifacts(checkout)
            generation = _sibling_row(checkout)
            host = _judgments_host((1,))
            host.generation_output = json.dumps(generation) + "\n"
            code, stdout, stderr = _invoke(
                ["eval", "run", "--slice", "judgments", "--stack", CI_STACK, "--ranked", str(ranked_path)],
                **_run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml"),
            )

        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertLess(stdout.index("ranked: ok"), stdout.index("preconditions: ok"))
        self.assertIn(
            f"generation {generation['gen_id']} ({generation['corpus_lockfile']}, {generation['state']})",
            stdout,
        )
        (argv, sql), = [
            (argv, sql) for argv, sql in host.calls if "FROM public.index_generations" in (sql or "")
        ]
        self.assertEqual(argv, tuple(command.record.reader_argv(command.stacks.CI_ROOT)))
        assert sql is not None
        bind = next(line for line in sql.splitlines() if line.startswith("\\set v_name "))
        self.assertEqual(read_psql_set(bind, "v_name"), generation["gen_id"])

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


class ResearchPreparation(unittest.TestCase):
    """Research checks the resolved generation and the selected stack before recording."""

    def setUp(self) -> None:
        self.checkout, _ = _fixture(self.enterContext(tempfile.TemporaryDirectory()), {1: RELEVANT_GRADE})
        self.models_path = ROOT / "models.lock"
        self.site_path = ROOT / "config/site.example.yaml"
        lock = models.load_models_lock(self.models_path).lock
        config = command.site.load_site(self.site_path).config
        assert lock is not None and config is not None
        self.profiles = lock.profiles
        profile = lock.profile(config.hardware_profile)
        assert profile is not None
        self.production_reranker = profile.model(RERANK.role) is not None

    def run_judgments(self, host: EvalHost, *flags: str) -> tuple[int, str, str]:
        return _invoke(
            ["eval", "run", "--slice", "judgments", *flags],
            models_path=self.models_path,
            **_run_kwargs(host, checkout=self.checkout, court_path=self.checkout / "courts.yaml"),
        )

    def test_registered_research_engine_slices_prepare_turns_and_hold_the_synthesis_judge(self) -> None:
        expected_hash = baselines.expected_config_hash(None, reranker=self.production_reranker)
        self.assertIsInstance(expected_hash, str)
        for name in ("research-qa-retrieval", "research-qa-synthesis"):
            with self.subTest(slice_name=name):
                spec = SLICE_RUNNERS[name]
                host = EvalHost()
                generation = _generation_row()
                host.generation_output = json.dumps(generation) + "\n"
                kwargs = _run_kwargs(host)
                rendered = Path(kwargs["rendered_dir"])
                target = command.engine.EngineTarget("fictitious-profile", "fictitious-model", 1000)
                held = fixture_judge_retry()
                runner = Mock(return_value=SliceResult(True, "Fictitious Research report\n", ()))
                with (
                    patch.dict(command.SLICE_RUNNERS, {name: replace(spec, runner=runner)}),
                    patch.object(command.nogpu, "is_no_gpu_host", return_value=False),
                    patch.object(command.engine, "resolve_engine_target", return_value=target) as resolve,
                    patch.object(command.sampling, "hold_running", return_value=held) as hold,
                    patch.object(command.research_door, "probe", return_value=None) as probe,
                ):
                    code, stdout, stderr = _invoke(
                        ["eval", "run", "--slice", name], **kwargs,
                    )
                self.assertEqual((code, stderr), (0, ""), stdout)
                self.assertIn("engine lock taken", stdout)
                self.assertIn(f"door {command.research_door.TURN_PATH} probed", stdout)
                resolve.assert_called_once()
                probe.assert_called_once_with(host, rendered, sibling=False)
                runner.assert_called_once()
                context = cast(RunContext, runner.call_args.args[2])
                assert context.generation is not None
                self.assertEqual(context.generation.name, generation["gen_id"])
                self.assertEqual(context.expected_config_hash, expected_hash)
                self.assertEqual(context.served_model_name, target.served_model_name)
                self.assertEqual(context.judge_prompt_id, spec.judge_prompt)
                if name == "research-qa-synthesis":
                    hold.assert_called_once()
                    self.assertIs(context.judge_retry, held)
                else:
                    hold.assert_not_called()
                    self.assertIsNone(context.judge_retry)
                self.assertEqual(host.locks, {})

    def test_production_without_a_reranker_and_ci_prepare_the_off_stage_hash(self) -> None:
        profile = next(profile for profile in self.profiles if profile.model(RERANK.role) is None)
        site_document = yaml.safe_load(self.site_path.read_text(encoding="utf-8"))
        path = self.checkout / "fictitious-site.yaml"
        site_document["hardware_profile"] = profile.name
        path.write_text(yaml.safe_dump(site_document), encoding="utf-8")
        row = _generation_row()
        surfaces = _ENGINE_RESEARCH_SURFACES - {CallSurface.ENGINE}
        expected_hash = baselines.expected_config_hash(None, reranker=False)
        assert isinstance(expected_hash, str)
        for stack_name, site_path in ((PRODUCTION_STACK, path), (CI_STACK, self.site_path)):
            with self.subTest(stack=stack_name), patch.object(
                baselines, "expected_config_hash", wraps=baselines.expected_config_hash,
            ) as config_hash:
                (code, stdout, stderr), contexts, _ = _invoke_planted(
                    EvalHost(), self.checkout, self.checkout / "fictitious-ranked.json", surfaces,
                    site_path=site_path, stack_name=stack_name, generation_name=row["gen_id"],
                )
                self.assertEqual((code, stderr), (0, ""), stdout)
                config_hash.assert_called_once_with(None, reranker=False)
                context, = contexts
                self.assertEqual(context.expected_config_hash, expected_hash)

    def test_built_generation_refusal_names_promotion_and_a_ranked_retry_on_the_same_stack(self) -> None:
        _copy_sibling_artifacts(self.checkout)
        for stack_name, row in (
            (PRODUCTION_STACK, _generation_row(state="built")),
            (CI_STACK, _sibling_row(self.checkout)),
        ):
            host = EvalHost()
            host.generation_output = json.dumps(row) + "\n"
            name = row["gen_id"]
            with (
                self.subTest(stack=stack_name),
                patch.object(command.research_door, "probe") as probe,
                patch.object(command.stacks.secrets, "select_directory"),
            ):
                flags = ("--stack", CI_STACK) if stack_name == CI_STACK else ()
                code, stdout, stderr = self.run_judgments(host, *flags, "--generation", name)
                probe.assert_not_called()
                self.assertEqual((code, stderr), (1, ""), stdout)
                self.assertIn(f"generation {name} is built and /turn requires a live generation", stdout)
                self.assertIn(report.command(f"index promote {name}"), stdout)
                stack_flag = f" --stack {CI_STACK}" if flags else ""
                self.assertIn(report.command(
                    f"eval run --slice judgments --ranked <file>{stack_flag} --generation {name}"
                ), stdout)
                self.assertEqual([line.split(":", 1)[0] for line in stdout.splitlines()], ["load", "preconditions"])
                self.assertEqual(host.lock_records, [])
                self.assertFalse(any(argv[0] == "git" for argv, _ in host.calls))

    def test_no_gpu_refuses_after_generation_resolution_and_before_the_probe(self) -> None:
        host = EvalHost()
        host.generation_output = json.dumps(_generation_row()) + "\n"
        with (
            patch.object(command.nogpu, "is_no_gpu_host", return_value=True),
            patch.object(command.research_door, "probe") as probe,
        ):
            code, stdout, stderr = self.run_judgments(host)
        probe.assert_not_called()
        self.assertEqual((code, stderr), (1, ""), stdout)
        self.assertIn("this host carries the no-GPU marker and serves no /turn", stdout)
        self.assertIn("Run the evaluation on a GPU host", stdout)
        (_argv, sql), = host.calls
        self.assertIn("FROM public.index_generations", sql or "")

    def test_expected_hash_refusal_precedes_the_research_probe(self) -> None:
        host = EvalHost()
        host.generation_output = json.dumps(_generation_row()) + "\n"
        refused = baselines.expected_config_hash({"fictitious-override": True}, reranker=self.production_reranker)
        assert isinstance(refused, command.Problem)
        for baseline in (None, *baselines.BASELINES):
            with (
                self.subTest(baseline=baseline),
                patch.object(baselines, "expected_config_hash", return_value=refused) as expected_hash,
                patch.object(command.research_door, "probe") as probe,
            ):
                flags = () if baseline is None else ("--baseline", baseline)
                code, stdout, stderr = self.run_judgments(host, *flags)
                expected_hash.assert_called_once_with(
                    None if baseline is None else baselines.BASELINES[baseline], reranker=self.production_reranker,
                )
                probe.assert_not_called()
                self.assertEqual((code, stderr), (1, ""), stdout)
                self.assertIn(refused.problem, stdout)
                self.assertIn(refused.fix, stdout)
                self.assertIn("checkout's retrieval whitelist", stdout)

    def test_retries_keep_the_baseline_for_research_and_drop_it_for_a_ranked_file(self) -> None:
        host = EvalHost()
        row = _generation_row(state="built")
        name = row["gen_id"]
        host.generation_output = json.dumps(row) + "\n"
        flags = ("--baseline", "off", "--generation", name)
        code, stdout, stderr = self.run_judgments(host, *flags)
        self.assertEqual((code, stderr), (1, ""), stdout)
        self.assertIn(report.command(f"eval run --slice judgments --ranked <file> --generation {name}"), stdout)
        self.assertNotIn("--baseline", stdout)

        host.generation_code = 23
        code, stdout, stderr = self.run_judgments(host, *flags)
        self.assertEqual((code, stderr), (1, ""), stdout)
        self.assertIn(report.command(f"eval run --slice judgments --baseline off --generation {name}") + " again.", stdout)

    def test_engine_checks_research_under_the_lock_after_target_and_generation(self) -> None:
        host = EvalHost()
        generation = _generation_row()
        checks: list[str] = []
        shipped_hash = baselines.expected_config_hash(None, reranker=False)
        assert isinstance(shipped_hash, str)

        def marker(io: EvalHost) -> bool:
            self.assertIs(io, host)
            if not checks:
                self.assertEqual(host.locks, {})
                checks.append("engine marker")
            else:
                self.assertIn(backuplock.ENGINE_LOCK.path, host.locks)
                cast(Mock, command.engine.resolve_engine_target).assert_called_once()
                self.assertTrue(any("FROM public.index_generations" in (sql or "") for _, sql in host.calls))
                checks.append("Research marker")
            return False

        def config_hash(turn_overrides: object, *, reranker: bool) -> str:
            self.assertIsNone(turn_overrides)
            self.assertIs(reranker, False)
            self.assertIn(backuplock.ENGINE_LOCK.path, host.locks)
            checks.append("config hash")
            return shipped_hash

        def probe(io: EvalHost, rendered_dir: Path, *, sibling: bool) -> None:
            self.assertIs(io, host)
            self.assertIn(backuplock.ENGINE_LOCK.path, host.locks)
            self.assertEqual(rendered_dir, Path(command.stacks.CI_ROOT))
            self.assertTrue(sibling)
            self.assertFalse(any(argv[0] == "git" or sql == "SELECT 1;\n" for argv, sql in host.calls))
            checks.append("Research probe")

        research_probe = Mock(side_effect=probe)
        with (
            patch.object(command.nogpu, "is_no_gpu_host", side_effect=marker),
            patch.object(baselines, "expected_config_hash", side_effect=config_hash),
        ):
            (code, stdout, stderr), contexts, _ = _invoke_planted(
                host, self.checkout, self.checkout / "fictitious-ranked.json", _ENGINE_RESEARCH_SURFACES,
                stack_name=CI_STACK, generation_name=generation["gen_id"], research_probe=research_probe,
            )
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertEqual(checks, ["engine marker", "Research marker", "config hash", "Research probe"])
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].expected_config_hash, shipped_hash)
        self.assertTrue(any(sql == "SELECT 1;\n" for _, sql in host.calls))
        self.assertEqual(host.locks, {})

    def test_engine_research_refusals_stop_before_the_runner_and_release_the_lock(self) -> None:
        built, live = _generation_row(state="built"), _generation_row()
        built_name = built["gen_id"]
        bad_hash = baselines.expected_config_hash({"fictitious-override": True}, reranker=self.production_reranker)
        shipped_hash = baselines.expected_config_hash(None, reranker=self.production_reranker)
        assert isinstance(bad_hash, command.Problem) and isinstance(shipped_hash, str)
        probe_refusal = command.Problem("Fictitious Research probe refused.", "Retry the fictitious service.")
        cases = (
            ("no-GPU", built, command.Problem(
                "this host carries the no-GPU marker and serves no /turn",
                "Run the evaluation on a GPU host, then retry.",
            )),
            ("built", built, command.Problem(
                f"generation {built_name} is built and /turn requires a live generation",
                f"Run {report.command(f'index promote {built_name}')}, then retry.",
            )),
            ("hash", live, bad_hash),
            ("probe", live, probe_refusal),
        )
        for refusal, generation, problem in cases:
            with self.subTest(refusal=refusal):
                host = EvalHost()
                research_probe = Mock(return_value=probe_refusal)
                flags = ("--baseline", "off") if refusal == "hash" else ()
                with (
                    patch.object(command.nogpu, "is_no_gpu_host", side_effect=(False, refusal == "no-GPU")),
                    patch.object(
                        baselines, "expected_config_hash", return_value=bad_hash if refusal == "hash" else shipped_hash,
                    ) as expected_hash,
                ):
                    (code, stdout, stderr), contexts, _ = _invoke_planted(
                        host, self.checkout, self.checkout / "fictitious-ranked.json", _ENGINE_RESEARCH_SURFACES,
                        generation_reply=json.dumps(generation) + "\n", flags=flags, research_probe=research_probe,
                    )
                self.assertEqual((code, stderr, contexts), (1, "", []), stdout)
                load, row = stdout.splitlines()
                self.assertTrue(load.startswith("load: ok"), load)
                self.assertEqual(row, f"preconditions: refuse — {problem.problem} Fix: {problem.fix}")
                self.assertEqual(expected_hash.call_count, int(refusal in {"hash", "probe"}))
                if refusal == "hash":
                    expected_hash.assert_called_once_with(baselines.BASELINES["off"], reranker=self.production_reranker)
                self.assertEqual(research_probe.call_count, int(refusal == "probe"))
                self.assertFalse(any(argv[0] == "git" or sql == "SELECT 1;\n" for argv, sql in host.calls))
                self.assertEqual([key for key, _ in host.lock_records], [backuplock.ENGINE_LOCK.path])
                self.assertEqual(host.locks, {})


class PreparationBoundaries(unittest.TestCase):
    """Registry surfaces and prepared record facts stay within their boundaries."""

    def test_retired_surface_flags_have_no_readers(self) -> None:
        forbidden = {"reaches_engine", "drives_turns", "takes_ranked", "reaches_box"}
        readers: list[str] = []
        for home in (ROOT / "gideon", ROOT / "tools", ROOT / "tests"):
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
