"""Contracts for the evaluation slice registry's call surfaces."""

import ast
import inspect
import shutil
import tempfile
import unittest
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

from test_evaluation_record import read_psql_set
from test_evaluation_run import NOW, ROOT, EvalHost, _invoke, _run_kwargs
from test_judgments_slice import _fixture, _ranked_file

from gideon.evaluation import command
from gideon.evaluation.evalset import SET_ROOT, LoadedSet
from gideon.evaluation.results import RunContext, SliceResult
from gideon.evaluation.slices import SLICE_RUNNERS, CallSurface


class SliceSurfaces(unittest.TestCase):
    """Each registry entry names the surfaces its runs reach."""

    def test_every_entry_names_its_surfaces_from_the_closed_list(self) -> None:
        for name, spec in SLICE_RUNNERS.items():
            with self.subTest(slice_name=name):
                self.assertIs(type(spec.surfaces), frozenset)
                self.assertTrue(all(type(surface) is CallSurface for surface in spec.surfaces))

    def test_reaches_box_matches_surface_membership(self) -> None:
        on_box = {surface for surface in CallSurface if surface.on_box}
        self.assertEqual(on_box, {CallSurface.ENGINE, CallSurface.IMAGE, CallSurface.TURNS})
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
) -> tuple[tuple[int, str, str], list[RunContext], command.engine.EngineTarget]:
    """Run one surface set through the command with observable seams."""

    contexts: list[RunContext] = []

    def run_slice(_loaded: LoadedSet, _name: str, context: RunContext) -> SliceResult:
        contexts.append(context)
        return SliceResult(True, "visibly fictitious runner report\n", ())

    target = command.engine.EngineTarget("fictitious-target-profile", "fictitious-model", 1)
    replacement = dict(SLICE_RUNNERS)
    replacement["judgments"] = replace(
        replacement["judgments"], surfaces=surfaces, runner=run_slice
    )
    argv = ["eval", "run", "--slice", "judgments", "--stack", "production"]
    if CallSurface.RANKED_FILE in surfaces:
        argv.extend(("--ranked", str(ranked_path)))
    if supplied_set is not None:
        argv.extend(("--set", str(supplied_set)))
    kwargs = _run_kwargs(host, checkout=checkout, court_path=checkout / "courts.yaml")
    kwargs["clock"] = lambda: NOW
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
        select_directory.assert_not_called()
    return outcome, contexts, target


class PreparationRoutes(unittest.TestCase):
    """Each named surface supplies its runner context and record facts."""

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
                    if CallSurface.IMAGE in surfaces:
                        assert context.image is not None
                        self.assertEqual(context.image.checkout, checkout)
                        self.assertIn("gideon@sha256:", context.image.reference)
                    self.assertEqual(
                        context.ranked is None, CallSurface.RANKED_FILE not in surfaces
                    )
                    sql = next(
                        input for argv, input in host.calls
                        if argv[0] == "docker" and input not in (None, "SELECT 1;\n")
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
