"""Engine stage, seeded sampling, and chat request contracts over fictitious inputs."""

import ast
import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from subprocess import TimeoutExpired
from unittest import TestCase
from unittest.mock import Mock, call, patch

from test_engine_verify import completed, models_text, site_text
from test_host_import_boundary import absolute_targets
from test_preflight import FakeHost

from gideon.evaluation import sampling, window
from gideon.host import engine, models, nogpu, report, site, stack, weights
from gideon.host.render.engine import GENERATOR
from gideon.host.render.services.model_server import model_command
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "example/fictitious-sampling-generator"
REVISION = "c" * 40
SEED = sampling.DEFAULT_SEED + 81
RENDERED = Path("/fictitious-sampling/rendered")
SITE_PATH = Path("/fictitious-sampling/site.yaml")
MODELS_PATH = Path("/fictitious-sampling/models.lock")
MODELS_ROOT = Path("/fictitious-sampling/weights")
CONFIG_PATH = weights.snapshot_path(REPOSITORY, REVISION, "generation_config.json", root=MODELS_ROOT)
CONTAINER = "fictitious-generator-container"
LOCATE = tuple(stack.compose_argv(RENDERED, "ps", "--status", "running", "--quiet", GENERATOR.service_name))
INSPECT = ("docker", "container", "inspect", "--format", "{{json .Config.Cmd}}", "--", CONTAINER)
PRIVATE = "fictitious private snapshot or command text"


def _source_trees(*directories: str) -> Iterator[tuple[Path, ast.Module]]:
    for directory in directories:
        for path in sorted((ROOT / directory).rglob("*.py")):
            yield path.relative_to(ROOT), ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _literal_counts(literal: str, *directories: str) -> dict[str, int]:
    return {
        path.as_posix(): count
        for path, tree in _source_trees(*directories)
        if (count := sum(
            node.value.count(literal)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ))
    }


def _named_calls(tree: ast.Module, name: str) -> Iterator[ast.Call]:
    nodes = tuple(ast.walk(tree))
    names = {name}
    names.update(
        alias.asname
        for node in nodes
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == name and alias.asname is not None
    )
    for node in nodes:
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id in names)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        ):
            yield node


class SourceHomes(TestCase):
    def test_chat_template_key_has_one_home(self) -> None:
        self.assertEqual(
            _literal_counts("chat_template_kwargs", "gideon/evaluation", "tools"),
            {"gideon/evaluation/sampling.py": 1},
        )

    def test_sha256_literal_has_one_home_outside_the_legs(self) -> None:
        counts = _literal_counts("[0-9a-f]{64}", "gideon/evaluation", "tools/segmenter", "tools/synthetic")
        home = "gideon/evaluation/evalset.py"
        self.assertEqual(counts.get(home), 1)
        self.assertEqual(set(counts), {
            home,
            "gideon/evaluation/pool_leg.py",
            "gideon/evaluation/segmenter_leg.py",
        })

    def test_tools_do_not_write_the_no_gpu_fix(self) -> None:
        self.assertEqual(_literal_counts("on the GPU box", "tools"), {})

    def test_quiet_window_judgement_has_three_callers(self) -> None:
        name = window.window_judgement.__name__
        self.assertEqual(
            {path.as_posix() for path, tree in _source_trees("gideon", "tools") if next(_named_calls(tree, name), None)},
            {"gideon/evaluation/command.py", "gideon/evaluation/sampling.py", "tools/turns/cli.py"},
        )

    def test_quiet_window_refusal_phrases_have_one_literal_home(self) -> None:
        for phrase in ("outside the quiet window: ", "add --force for an announced window"):
            with self.subTest(phrase=phrase):
                self.assertEqual(_literal_counts(phrase, "gideon", "tools"), {"gideon/evaluation/window.py": 1})

    def test_box_kits_do_not_import_window_or_zoneinfo(self) -> None:
        blocked = {window.__name__, "zoneinfo"}
        for relative in (
            "tools/segmenter/draft.py",
            "tools/synthetic/draft.py",
            "tools/judgments/assess.py",
            "tools/judgments/enginepin.py",
            "tools/signoffs/draft.py",
        ):
            if absent_from_export(relative, ROOT):
                continue
            with self.subTest(path=relative):
                path = ROOT / relative
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
                imports = [
                    (node.lineno, target)
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for target in absolute_targets(node, path)
                    if target in blocked
                ]
                self.assertEqual(imports, [])


class ChatRequest(TestCase):
    def test_greedy_request_keeps_the_wire_order_and_turns_thinking_off(self) -> None:
        model = "fictitious-generator"
        system_text = "Fictitious system text"
        user_text = "Fictitious user text"
        max_tokens = 73
        schema_name = "fictitious_answer"
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        body = sampling.chat_request(
            model, system_text=system_text, user_text=user_text, max_tokens=max_tokens,
            schema_name=schema_name, schema=schema, enable_thinking=False,
        )
        self.assertEqual(json.dumps(body), json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema},
            },
            "chat_template_kwargs": {"enable_thinking": False},
        }))

    def test_supplied_sampling_fields_keep_their_values_and_order_with_thinking_on(self) -> None:
        fictitious_fields = {"seed": 97, "top_k": 13, "top_p": 0.7}
        for fields in (fictitious_fields, {}):
            with self.subTest(fields=fields):
                body = sampling.chat_request(
                    "fictitious-generator", system_text="Fictitious system text", user_text="Fictitious user text",
                    max_tokens=73, schema_name="fictitious_answer", schema={"type": "object"},
                    enable_thinking=True, sampling_fields=fields,
                )
                self.assertEqual(tuple(body), (
                    "model", "messages", "max_tokens", *fields, "stream", "response_format", "chat_template_kwargs",
                ))
                self.assertEqual({key: body[key] for key in fields}, fields)
                self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": True})


class SamplingConfig(TestCase):
    def test_equal_config_and_equal_int_and_float(self) -> None:
        config = dict(sampling.THINKING_SAMPLING)
        self.assertEqual(sampling.sampling_differences(config), ())
        self.assertEqual(sampling.sampling_differences({**config, "top_k": float(config["top_k"])}), ())

    def test_reports_each_differing_key_in_request_order(self) -> None:
        config = dict(sampling.THINKING_SAMPLING)
        for key, value in config.items():
            with self.subTest(key=key):
                self.assertEqual(sampling.sampling_differences({**config, key: value + 1}), (key,))
        self.assertEqual(
            sampling.sampling_differences({key: config[key] + 1 for key in reversed(config)}), tuple(config),
        )

    def test_missing_required_keys_and_optional_min_p(self) -> None:
        config = dict(sampling.THINKING_SAMPLING)
        del config["min_p"]
        self.assertEqual(sampling.sampling_differences(config), ())
        for key in config:
            with self.subTest(key=key):
                missing = {name: value for name, value in config.items() if name != key}
                self.assertEqual(sampling.sampling_differences(missing), (key,))

    def test_booleans_are_refused_even_when_numerically_equal(self) -> None:
        config = dict(sampling.THINKING_SAMPLING)
        for key, value in config.items():
            with self.subTest(key=key):
                self.assertEqual(sampling.sampling_differences({**config, key: bool(value)}), (key,))


class SnapshotHold(TestCase):
    def test_seed_successor_advances_and_wraps(self) -> None:
        self.assertEqual(sampling.next_seed(SEED), SEED + 1)
        self.assertEqual(sampling.next_seed(sampling.MAX_SEED - 1), sampling.MAX_SEED)
        self.assertEqual(sampling.next_seed(sampling.MAX_SEED), 0)

    def test_retry_derives_the_same_sampling_fields_at_the_next_seed(self) -> None:
        config = json.dumps(dict(sampling.THINKING_SAMPLING))
        for seed, successor in ((SEED, SEED + 1), (sampling.MAX_SEED, 0)):
            with self.subTest(seed=seed):
                host = FakeHost(files={str(CONFIG_PATH): config})
                held = sampling.hold(host, REPOSITORY, REVISION, seed, models_root=MODELS_ROOT)
                assert isinstance(held, sampling.HeldSet), held
                retry = held.for_retry()
                self.assertEqual(retry.seed, successor)
                self.assertEqual(retry.request_fields, {**json.loads(config), "seed": successor})
                self.assertEqual(held.request_fields, {**json.loads(config), "seed": seed})

    def test_equal_snapshot_with_or_without_min_p_holds_the_same_request(self) -> None:
        for omit_min_p in (False, True):
            with self.subTest(omit_min_p=omit_min_p):
                config = dict(sampling.THINKING_SAMPLING)
                if omit_min_p:
                    del config["min_p"]
                host = FakeHost(files={str(CONFIG_PATH): json.dumps(config)})
                held = sampling.hold(host, REPOSITORY, REVISION, SEED, models_root=MODELS_ROOT)
                assert isinstance(held, sampling.HeldSet), held
                self.assertEqual(held.seed, SEED)
                self.assertEqual(held.request_fields, {**sampling.THINKING_SAMPLING, "seed": SEED})
                with self.assertRaises(FrozenInstanceError):
                    held.seed = SEED + 1  # type: ignore[misc]

    def test_differences_name_only_the_keys_and_the_sampling_home(self) -> None:
        for key, value in (("top_k", sampling.THINKING_SAMPLING["top_k"] + 1), ("temperature", PRIVATE)):
            with self.subTest(key=key):
                host = FakeHost(files={str(CONFIG_PATH): json.dumps({**sampling.THINKING_SAMPLING, key: value})})
                problem = sampling.hold(host, REPOSITORY, REVISION, SEED, models_root=MODELS_ROOT)
                assert isinstance(problem, report.Problem), problem
                self.assertEqual(problem.problem, f"sampling keys differ from the pinned generation_config.json: {key}")
                self.assertIn("serving the reference profile's generator", problem.fix)
                self.assertIn("gideon/evaluation/sampling.py", problem.fix)
                self.assertNotIn(PRIVATE, repr(problem))

    def test_unreadable_or_malformed_snapshot_names_models_pull_in_the_run_form(self) -> None:
        valid = json.dumps(dict(sampling.THINKING_SAMPLING))
        duplicate = '{"temperature": ' + json.dumps(PRIVATE) + ", " + valid[1:]
        for label, host in (
            ("unreadable", FakeHost(read_errors={str(CONFIG_PATH): PermissionError(PRIVATE)})),
            ("not an object", FakeHost(files={str(CONFIG_PATH): json.dumps([PRIVATE])})),
            ("malformed", FakeHost(files={str(CONFIG_PATH): "{" + PRIVATE})),
            ("duplicate", FakeHost(files={str(CONFIG_PATH): duplicate})),
        ):
            for installed, command in ((False, "sudo python3 -m gideon models pull"), (True, "gideon models pull")):
                with self.subTest(label=label, installed=installed), patch.object(report, "_installed_form", installed):
                    problem = sampling.hold(host, REPOSITORY, REVISION, SEED, models_root=MODELS_ROOT)
                    self.assertEqual(problem, report.Problem(
                        "the pinned generation_config.json cannot be read as a JSON object",
                        f"Run {command} to restore the pinned snapshot, then retry.",
                    ))

    def test_only_the_sampling_home_constructs_a_held_set(self) -> None:
        home = Path("gideon/evaluation/sampling.py")
        constructor = sampling.HeldSet.__name__
        found = [
            f"{path}:{node.lineno}"
            for path, tree in _source_trees("gideon", "tools")
            if path != home
            for node in _named_calls(tree, constructor)
        ]
        self.assertEqual(found, [])


class TargetCheck(TestCase):
    tool = "the fictitious assessor"
    work = "fictitious grading"
    inside = datetime(2099, 1, 3, 12, tzinfo=UTC)
    outside = datetime(2099, 1, 5, 18, tzinfo=UTC)

    def test_marker_refuses_before_site_load_window_or_target_resolution(self) -> None:
        host = FakeHost(files={str(nogpu.NO_GPU_PATH): "fictitious no-GPU marker"})
        with (
            patch.object(site, "load_site") as load,
            patch.object(window, "window_judgement") as judge,
            patch.object(engine, "resolve_engine_target") as resolve,
        ):
            problem = sampling.check_target(
                host, RENDERED, SITE_PATH, MODELS_PATH,
                tool=self.tool, work=self.work, instant=self.inside, force=False,
            )
        self.assertEqual(problem, report.Problem(
            f"the no-GPU marker refuses {self.work}", f"Run {self.tool} on the GPU box, then retry.",
        ))
        load.assert_not_called()
        judge.assert_not_called()
        resolve.assert_not_called()

    def test_site_refusal_keeps_the_first_fix_and_skips_window_and_resolution(self) -> None:
        host = FakeHost()
        errors = (
            site.SiteError(None, PRIVATE, "Repair the first fictitious site error."),
            site.SiteError(None, "Another fictitious site error", "Repair the second fictitious site error."),
        )
        for result, fix in (
            (site.SiteLoadResult(errors=errors), errors[0].fix),
            (site.SiteLoadResult(), "Restore the site file, then retry."),
        ):
            with (
                self.subTest(result=result),
                patch.object(site, "load_site", return_value=result) as load,
                patch.object(window, "window_judgement") as judge,
                patch.object(engine, "resolve_engine_target") as resolve,
            ):
                problem = sampling.check_target(
                    host, RENDERED, str(SITE_PATH), MODELS_PATH,
                    tool=self.tool, work=self.work, instant=self.inside, force=False,
                )
                self.assertEqual(problem, report.Problem("the site file cannot be loaded", fix))
                load.assert_called_once_with(SITE_PATH, host=host)
                judge.assert_not_called()
                resolve.assert_not_called()

    def test_resolution_returns_the_frozen_target_and_detail_and_forwards_profile_and_sleep(self) -> None:
        profile = "fictitious-profile"
        host = FakeHost(files={str(SITE_PATH): site_text(profile)})
        target = engine.EngineTarget(profile, "fictitious-model", 4096)
        sleep = Mock()
        with patch.object(engine, "resolve_engine_target", return_value=target) as resolve:
            result = sampling.check_target(
                host, RENDERED, SITE_PATH, MODELS_PATH,
                tool=self.tool, work=self.work, instant=self.inside, force=False, sleep=sleep,
            )
        assert isinstance(result, sampling.CheckedTarget), result
        self.assertIs(result.target, target)
        self.assertEqual(result.window_detail, f"weekend ({self.inside.strftime('%A')})")
        with self.assertRaises(FrozenInstanceError):
            result.window_detail = "fictitious replacement detail"  # type: ignore[misc]
        resolve.assert_called_once_with(host, RENDERED, hardware_profile=profile, models_path=MODELS_PATH, sleep=sleep)

    def test_outside_without_force_returns_the_exact_window_refusal_before_resolution(self) -> None:
        host = FakeHost(files={str(SITE_PATH): site_text()})
        config = site.load_site(SITE_PATH, host=host).config
        assert config is not None
        judgement = window.window_judgement(self.outside, config.office.timezone)
        self.assertFalse(judgement.inside)
        with patch.object(engine, "resolve_engine_target") as resolve:
            result = sampling.check_target(
                host, RENDERED, SITE_PATH, MODELS_PATH,
                tool=self.tool, work=self.work, instant=self.outside, force=False,
            )
        self.assertEqual(result, report.Problem(*window.refusal_text(judgement)))
        resolve.assert_not_called()

    def test_outside_with_force_returns_the_target_and_forced_detail(self) -> None:
        host = FakeHost(files={str(SITE_PATH): site_text()})
        target = engine.EngineTarget("fictitious-profile", "fictitious-model", 4096)
        with patch.object(engine, "resolve_engine_target", return_value=target) as resolve:
            result = sampling.check_target(
                host, RENDERED, SITE_PATH, MODELS_PATH,
                tool=self.tool, work=self.work, instant=self.outside, force=True,
            )
        assert isinstance(result, sampling.CheckedTarget), result
        self.assertIs(result.target, target)
        self.assertTrue(result.window_detail.startswith("office hours ("))
        self.assertTrue(result.window_detail.endswith(", forced"))
        resolve.assert_called_once()

    def test_one_instant_is_inside_or_outside_according_to_the_site_zone(self) -> None:
        fixture = site_text()
        config = site.load_site_text(fixture).config
        assert config is not None
        target = engine.EngineTarget(config.hardware_profile, "fictitious-model", 4096)
        for zone, inside in (("Etc/UTC", False), ("Asia/Tokyo", True)):
            with self.subTest(zone=zone), patch.object(engine, "resolve_engine_target", return_value=target) as resolve:
                host = FakeHost(files={str(SITE_PATH): fixture.replace(
                    f"timezone: {config.office.timezone}", f"timezone: {zone}",
                )})
                result = sampling.check_target(
                    host, RENDERED, SITE_PATH, MODELS_PATH,
                    tool=self.tool, work=self.work, instant=self.outside, force=False,
                )
                judgement = window.window_judgement(self.outside, zone)
                if inside:
                    self.assertEqual(result, sampling.CheckedTarget(target, judgement.description))
                    resolve.assert_called_once()
                else:
                    self.assertEqual(result, report.Problem(*window.refusal_text(judgement)))
                    resolve.assert_not_called()

    def test_resolver_exceptions_are_refused_with_the_callers_pin_fix(self) -> None:
        host = FakeHost(files={str(SITE_PATH): site_text()})
        for error in (FileNotFoundError(PRIVATE), TimeoutExpired("fictitious-compose", 2, stderr=PRIVATE)):
            with self.subTest(error=error), patch.object(engine, "resolve_engine_target", side_effect=error) as resolve:
                problem = sampling.check_target(
                    host, RENDERED, SITE_PATH, MODELS_PATH,
                    tool=self.tool, work=self.work, instant=self.inside, force=False,
                )
                self.assertEqual(problem, report.Problem(
                    "the generator target check could not run",
                    f"Run {report.command('apply')} from the checkout holding the intended model pin, "
                    f"or run {self.tool} from the checkout the box runs.",
                ))
                resolve.assert_called_once()

    def test_failed_compose_streams_stay_out_of_the_target_refusal(self) -> None:
        stdout, stderr = "fictitious private stdout", "fictitious private stderr"
        ps = tuple(stack.compose_argv(RENDERED, "ps", "--all", "--format", "json"))
        lock_text = models_text()
        lock = models.load_models_lock_text(lock_text).lock
        assert lock is not None
        host = FakeHost(
            files={
                str(SITE_PATH): site_text(lock.reference), str(MODELS_PATH): lock_text,
                str(RENDERED / "compose.yaml"): f"services:\n  {GENERATOR.service_name}: {{}}\n",
            },
            commands={ps: completed(ps, returncode=17, stdout=stdout, stderr=stderr)},
        )
        resolved = engine.resolve_engine_target(
            host, RENDERED, hardware_profile=lock.reference, models_path=MODELS_PATH, sleep=Mock(),
        )
        assert isinstance(resolved, report.Problem)
        self.assertIn(stdout, resolved.problem)
        self.assertIn(stderr, resolved.problem)
        host.calls.clear()
        problem = sampling.check_target(
            host, RENDERED, SITE_PATH, MODELS_PATH,
            tool=self.tool, work=self.work, instant=self.inside, force=False,
        )
        self.assertEqual(problem, report.Problem(
            "the generator target is unavailable", resolved.fix,
        ))
        self.assertEqual(resolved.fix, stack.logs_fix(RENDERED, GENERATOR.service_name))
        self.assertEqual(host.calls, [ps])
        self.assertNotIn(stdout, repr(problem))
        self.assertNotIn(stderr, repr(problem))


class RunningPin(TestCase):
    def test_reads_the_pin_from_the_start_command(self) -> None:
        command = json.dumps([REPOSITORY, "--revision", REVISION, "--fictitious-option"])
        host = FakeHost(commands={LOCATE: completed(LOCATE, stdout=f"{CONTAINER}\n"), INSPECT: completed(INSPECT, stdout=command)})
        self.assertEqual(sampling.running_pin(host, RENDERED, "Use the fictitious fix."), (REPOSITORY, REVISION))
        self.assertEqual(host.calls, [LOCATE, INSPECT])

    def test_lookup_and_inspection_failures_keep_the_callers_fix(self) -> None:
        located = completed(LOCATE, stdout=f"{CONTAINER}\n", stderr=PRIVATE)
        command = json.dumps([REPOSITORY, "--revision", REVISION])
        fix = "Use the fictitious pin fix."
        for replies, problem in (
            ((FileNotFoundError(PRIVATE),), "generator container lookup could not run"),
            ((completed(LOCATE, returncode=23, stdout=CONTAINER, stderr=PRIVATE),), "generator container lookup exited 23"),
            ((completed(LOCATE),), "expected one running generator container"),
            ((completed(LOCATE, stdout=f"{CONTAINER}\n{PRIVATE}"),), "expected one running generator container"),
            ((located, TimeoutExpired(INSPECT, 2, stderr=PRIVATE)), "generator container inspection could not run"),
            ((located, completed(INSPECT, returncode=19, stdout=command, stderr=PRIVATE)), "generator container inspection exited 19"),
            ((located, completed(INSPECT, stdout=PRIVATE)), "the running generator command cannot be read"),
        ):
            host = FakeHost()
            with self.subTest(problem=problem), patch.object(host, "run", side_effect=replies) as run:
                self.assertEqual(sampling.running_pin(host, RENDERED, fix), report.Problem(problem, fix))
                self.assertEqual(run.call_args_list, [call(list(LOCATE)), call(list(INSPECT))][:len(replies)])

    def test_start_command_requires_one_well_formed_positional_revision(self) -> None:
        valid = [REPOSITORY, "--revision", REVISION]
        fix = "Use the fictitious pin fix."
        for command in (
            {"arguments": valid}, valid[:-1], [*valid, 9], [REPOSITORY, "--fictitious-option", REVISION],
            [PRIVATE, *valid[1:]], [*valid[:2], PRIVATE], [*valid, "--revision", "d" * 40],
            [*valid, f"--revision={'d' * 40}"],
        ):
            with self.subTest(command=command):
                host = FakeHost(commands={
                    LOCATE: completed(LOCATE, stdout=CONTAINER), INSPECT: completed(INSPECT, stdout=json.dumps(command)),
                })
                self.assertEqual(sampling.running_pin(host, RENDERED, fix), report.Problem(
                    "the running generator command has no usable repository and revision", fix,
                ))


class RunningHold(TestCase):
    def setUp(self) -> None:
        self.lock_text = models_text()
        lock = models.load_models_lock_text(self.lock_text).lock
        assert lock is not None
        profile = lock.profile(lock.reference)
        assert profile is not None
        self.profile = profile.name
        pin = profile.model(GENERATOR.role)
        assert pin is not None
        self.pin = pin
        self.config = weights.snapshot_path(pin.repo, pin.revision, "generation_config.json", root=MODELS_ROOT)

    def host(self, *, revision: str | None = None) -> FakeHost:
        command = model_command(self.pin, GENERATOR)
        if revision is not None:
            command[2] = revision
        return FakeHost(
            files={str(MODELS_PATH): self.lock_text, str(self.config): json.dumps(dict(sampling.THINKING_SAMPLING))},
            commands={LOCATE: completed(LOCATE, stdout=CONTAINER), INSPECT: completed(INSPECT, stdout=json.dumps(command))},
        )

    def test_equal_running_pin_holds_the_selected_snapshot(self) -> None:
        host = self.host()
        with patch.object(host, "read_text", wraps=host.read_text) as read:
            held = sampling.hold_running(host, RENDERED, MODELS_PATH, self.profile, SEED, models_root=MODELS_ROOT)
        assert isinstance(held, sampling.HeldSet), held
        self.assertEqual(held.request_fields, {**sampling.THINKING_SAMPLING, "seed": SEED})
        self.assertEqual(host.calls, [LOCATE, INSPECT])
        self.assertEqual(read.call_args_list, [call(MODELS_PATH), call(self.config)])

    def test_different_running_revision_refuses_before_reading_the_snapshot(self) -> None:
        other = "f" if self.pin.revision[0] != "f" else "e"
        host = self.host(revision=other + self.pin.revision[1:])
        with patch.object(host, "read_text", wraps=host.read_text) as read:
            problem = sampling.hold_running(host, RENDERED, MODELS_PATH, self.profile, SEED, models_root=MODELS_ROOT)
        self.assertEqual(problem, report.Problem(
            "the running generator repository or revision differs from the checkout's pin",
            f"Run {report.command('apply')} from the checkout holding the intended model pin, "
            "or run this command from the checkout the box runs.",
        ))
        self.assertEqual(read.call_args_list, [call(MODELS_PATH)])

    def test_unknown_profile_refuses_before_reading_the_snapshot(self) -> None:
        host = self.host()
        with patch.object(host, "read_text", wraps=host.read_text) as read:
            problem = sampling.hold_running(
                host, RENDERED, MODELS_PATH, "fictitious-unknown-profile", SEED, models_root=MODELS_ROOT,
            )
        self.assertEqual(problem, report.Problem(
            "the generator pin cannot be read", "Restore the selected profile's generator pin, then retry.",
        ))
        self.assertEqual(read.call_args_list, [call(MODELS_PATH)])

    def test_pin_check_returns_the_running_pin_without_reading_the_snapshot(self) -> None:
        host = self.host()
        with patch.object(host, "read_text", wraps=host.read_text) as read:
            result = sampling.check_pin(host, RENDERED, MODELS_PATH, self.profile)
        self.assertEqual(result, (self.pin.repo, self.pin.revision))
        self.assertEqual(host.calls, [LOCATE, INSPECT])
        read.assert_called_once_with(MODELS_PATH)

    def test_running_pin_refusal_precedes_an_unreadable_lock(self) -> None:
        host = FakeHost(commands={LOCATE: completed(LOCATE, returncode=23, stdout=CONTAINER, stderr=PRIVATE)})
        tool = "the fictitious draft"
        with patch.object(host, "read_text", wraps=host.read_text) as read:
            result = sampling.check_pin(host, RENDERED, MODELS_PATH, self.profile, tool=tool)
        self.assertEqual(result, report.Problem(
            "generator container lookup exited 23",
            f"Run {report.command('apply')} from the checkout holding the intended model pin, "
            f"or run {tool} from the checkout the box runs.",
        ))
        self.assertEqual(host.calls, [LOCATE])
        read.assert_not_called()

    def test_pin_mismatch_names_the_callers_tool(self) -> None:
        other = "f" if self.pin.revision[0] != "f" else "e"
        host = self.host(revision=other + self.pin.revision[1:])
        tool = "the fictitious draft"
        self.assertEqual(sampling.check_pin(host, RENDERED, MODELS_PATH, self.profile, tool=tool), report.Problem(
            "the running generator repository or revision differs from the checkout's pin",
            f"Run {report.command('apply')} from the checkout holding the intended model pin, "
            f"or run {tool} from the checkout the box runs.",
        ))

    def test_running_hold_keeps_the_default_fix_for_a_running_pin_refusal(self) -> None:
        host = FakeHost(commands={LOCATE: completed(LOCATE, returncode=23, stdout=CONTAINER, stderr=PRIVATE)})
        self.assertEqual(
            sampling.hold_running(host, RENDERED, MODELS_PATH, self.profile, SEED, models_root=MODELS_ROOT),
            report.Problem(
                "generator container lookup exited 23",
                f"Run {report.command('apply')} from the checkout holding the intended model pin, "
                "or run this command from the checkout the box runs.",
            ),
        )
