"""Seeded sampling holds over fictitious snapshot and container replies."""

import ast
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from subprocess import TimeoutExpired
from unittest import TestCase
from unittest.mock import call, patch

from test_engine_verify import completed, models_text
from test_preflight import FakeHost

from gideon.evaluation import sampling
from gideon.host import models, report, stack, weights
from gideon.host.render.engine import GENERATOR
from gideon.host.render.services.model_server import model_command

REPOSITORY = "example/fictitious-sampling-generator"
REVISION = "c" * 40
SEED = sampling.DEFAULT_SEED + 81
RENDERED = Path("/fictitious-sampling/rendered")
MODELS_PATH = Path("/fictitious-sampling/models.lock")
MODELS_ROOT = Path("/fictitious-sampling/weights")
CONFIG_PATH = weights.snapshot_path(REPOSITORY, REVISION, "generation_config.json", root=MODELS_ROOT)
CONTAINER = "fictitious-generator-container"
LOCATE = tuple(stack.compose_argv(RENDERED, "ps", "--status", "running", "--quiet", GENERATOR.service_name))
INSPECT = ("docker", "container", "inspect", "--format", "{{json .Config.Cmd}}", "--", CONTAINER)
PRIVATE = "fictitious private snapshot or command text"


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
        root = Path(__file__).resolve().parents[1]
        home = root / "gideon/evaluation/sampling.py"
        constructor = sampling.HeldSet.__name__
        found = []
        for directory in (root / "gideon", root / "tools"):
            for path in sorted(directory.rglob("*.py")):
                if path == home:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and (
                        (isinstance(node.func, ast.Name) and node.func.id == constructor)
                        or (isinstance(node.func, ast.Attribute) and node.func.attr == constructor)
                    ):
                        found.append(f"{path.relative_to(root)}:{node.lineno}")
        self.assertEqual(found, [])


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
