"""Contract tests for the standard-library mask and its fixed root stage."""

from __future__ import annotations

import ast
import contextlib
import io
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from tools import mask

ROOT = Path(__file__).resolve().parent.parent


class RecordingRunner:
    """Record injected commands and return their scripted exit codes."""

    def __init__(self, codes: Sequence[int] = ()) -> None:
        self.codes = list(codes)
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str]) -> int:
        self.commands.append(tuple(command))
        return self.codes[len(self.commands) - 1] if self.codes else 0


def facts(
    *,
    present: Sequence[str] = (),
    directories: Sequence[str] = (),
    mounts: Sequence[str] = (),
    entries: Mapping[str, Sequence[str]] | None = None,
    null_device: int = 90,
    devices: Mapping[str, int] | None = None,
    uid: int = 1000,
    gid: int = 1000,
    groups: Sequence[int] = (1000, 1001),
    environment: Mapping[str, str] | None = None,
    password_name: str = "fictitious-user",
    password_home: str = "/home/fictitious-user",
    missing_tools: Sequence[str] = (),
    executable_paths: Mapping[str, str] | None = None,
    working_directory: str = "/workspace/checkout",
    aliases: Mapping[str, str] | None = None,
    runner: RecordingRunner | None = None,
) -> tuple[mask.Facts, RecordingRunner]:
    """Build facts without consulting the test process's filesystem."""

    alias_map = dict(aliases or {})

    def realpath(path: str) -> str:
        return alias_map.get(path, path)

    canonical_present = {realpath(path) for path in present}
    canonical_directories = {realpath(path) for path in directories}
    canonical_mounts = {realpath(path) for path in mounts}
    canonical_entries = {
        realpath(path): tuple(names) for path, names in (entries or {}).items()
    }
    device_map = {realpath(path): value for path, value in (devices or {}).items()}
    caller_environment = dict(environment or {})
    missing = set(missing_tools)
    executable_map = dict(executable_paths or {})
    command_runner = runner or RecordingRunner()
    password = cast(
        pwd.struct_passwd,
        SimpleNamespace(pw_name=password_name, pw_dir=password_home),
    )

    def device(path: str) -> int:
        return device_map.get(realpath(path), null_device if path == "/dev/null" else 10)

    def which(tool: str) -> str | None:
        if tool in missing:
            return None
        return executable_map.get(tool, f"/usr/bin/{tool}")

    return (
        mask.Facts(
            exists=lambda path: realpath(path) in canonical_present,
            is_directory=lambda path: realpath(path) in canonical_directories,
            is_mount=lambda path: realpath(path) in canonical_mounts,
            list_directory=lambda path: canonical_entries.get(realpath(path), ()),
            device=device,
            realpath=realpath,
            effective_uid=lambda: uid,
            effective_gid=lambda: gid,
            supplementary_groups=lambda: tuple(groups),
            password_entry=lambda _uid: password,
            environment=lambda: dict(caller_environment),
            which=which,
            working_directory=lambda: working_directory,
            run=command_runner,
        ),
        command_runner,
    )


def all_present_facts(
    *,
    environment: Mapping[str, str] | None = None,
    runner: RecordingRunner | None = None,
) -> tuple[mask.Facts, RecordingRunner]:
    """Return facts with every listed path present and already masked."""

    sockets = {path for path in mask.MASK_PATHS if path.endswith("docker.sock")}
    directories = tuple(path for path in mask.MASK_PATHS if path not in sockets)
    aliases = {sockets.pop(): next(iter(sockets))} if len(sockets) == 2 else {}
    return facts(
        present=mask.MASK_PATHS,
        directories=directories,
        mounts=directories,
        entries={},
        devices=dict.fromkeys(sockets, 90),
        environment=environment,
        aliases=aliases,
        runner=runner,
    )


def wrapped_parts(
    command: Sequence[str], paths: Sequence[str], test_facts: mask.Facts
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split a wrapped argv into its environment, paths, and command tails."""

    wrapped = mask.wrap(command, paths=paths, facts=test_facts)
    stage = wrapped.index(mask.ROOT_STAGE)
    environment_count = int(wrapped[stage + 5])
    path_count = int(wrapped[stage + 6])
    first = stage + 7
    environment = wrapped[first : first + environment_count]
    listed_paths = wrapped[first + environment_count : first + environment_count + path_count]
    return environment, listed_paths, wrapped[first + environment_count + path_count :]


class Refusals(unittest.TestCase):
    """The four probe refusals are ordered and carry separate fixes."""

    def assert_refused(self, result: mask.ProbeResult) -> mask.Refusal:
        self.assertIs(result.state, mask.ProbeState.REFUSED)
        self.assertIsNotNone(result.refusal)
        refusal = cast(mask.Refusal, result.refusal)
        self.assertTrue(refusal.problem)
        self.assertTrue(refusal.fix)
        self.assertNotIn("Fix:", refusal.problem)
        self.assertNotIn("Fix:", refusal.fix)
        return refusal

    def ready_facts(
        self,
        *,
        uid: int = 1000,
        missing_tools: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
        executable_paths: Mapping[str, str] | None = None,
        working_directory: str = "/workspace/checkout",
        runner: RecordingRunner | None = None,
    ) -> tuple[mask.Facts, RecordingRunner]:
        path = mask.MASK_PATHS[0]
        return facts(
            present=(path,),
            directories=(path,),
            mounts=(path,),
            entries={path: ("visible",)},
            uid=uid,
            missing_tools=missing_tools,
            environment=environment,
            executable_paths=executable_paths,
            working_directory=working_directory,
            runner=runner,
        )

    def test_root_refusal_precedes_tool_checks(self) -> None:
        test_facts, runner = self.ready_facts(uid=0, missing_tools=mask.REQUIRED_TOOLS)
        refusal = self.assert_refused(mask.probe(("pytest",), test_facts))
        self.assertIn("root", refusal.problem)
        self.assertEqual(runner.commands, [])

    def test_missing_tool_refusal_precedes_path_and_trial_checks(self) -> None:
        test_facts, runner = self.ready_facts(
            missing_tools=(mask.REQUIRED_TOOLS[0],),
            working_directory=mask.MASK_PATHS[0],
        )
        refusal = self.assert_refused(mask.probe(("pytest",), test_facts))
        self.assertIn(mask.REQUIRED_TOOLS[0], refusal.problem)
        self.assertEqual(runner.commands, [])

    def test_path_refusals_precede_the_trial(self) -> None:
        path_cases = (
            ("working directory", mask.MASK_PATHS[0], None, None),
            ("command executable", "/workspace/checkout", {"pytest": mask.MASK_PATHS[0] + "/pytest"}, None),
            ("TMPDIR", "/workspace/checkout", None, {"TMPDIR": mask.MASK_PATHS[0] + "/tmp"}),
        )
        for label, working_directory, executable_paths, environment in path_cases:
            with self.subTest(label=label):
                test_facts, runner = self.ready_facts(
                    working_directory=working_directory,
                    executable_paths=executable_paths,
                    environment=environment,
                )
                refusal = self.assert_refused(mask.probe(("pytest",), test_facts))
                self.assertIn(label, refusal.problem)
                self.assertEqual(runner.commands, [])

    def test_trial_refusal_is_last_and_has_its_fix(self) -> None:
        test_facts, runner = self.ready_facts(runner=RecordingRunner([23]))
        refusal = self.assert_refused(mask.probe(("pytest",), test_facts))
        self.assertIn("trial", refusal.problem)
        self.assertIn("sudo -v", refusal.fix)
        self.assertEqual(len(runner.commands), 1)

    def test_main_renders_a_refusal_as_problem_then_fix(self) -> None:
        runner = RecordingRunner([23])
        test_facts, _runner = self.ready_facts(runner=runner)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = mask.main(["--", "pytest"], test_facts)
        self.assertEqual(code, 1)
        self.assertRegex(stderr.getvalue(), r"^mask: .+\. Fix: .+\n$")


class Observation(unittest.TestCase):
    """Observation must prove an empty mount or null device, never claim it."""

    def test_all_present_paths_masked_means_inside(self) -> None:
        test_facts, _runner = all_present_facts()
        observed = mask.observe(test_facts)
        self.assertIs(observed.state, mask.ProbeState.INSIDE)
        expected = tuple(dict.fromkeys(test_facts.realpath(path) for path in mask.MASK_PATHS))
        self.assertEqual(observed.present, expected)
        self.assertEqual(observed.masked, observed.present)

    def test_a_non_empty_mount_is_not_masked(self) -> None:
        path = mask.MASK_PATHS[0]
        test_facts, _runner = facts(
            present=(path,),
            directories=(path,),
            mounts=(path,),
            entries={path: ("not-empty",)},
        )
        observed = mask.observe(test_facts)
        self.assertIs(observed.state, mask.ProbeState.READY)
        self.assertEqual(observed.masked, ())

    def test_no_present_path_is_absent_not_inside(self) -> None:
        test_facts, _runner = facts()
        observed = mask.observe(test_facts)
        self.assertIs(observed.state, mask.ProbeState.ABSENT)
        self.assertEqual(observed.present, ())

    def test_a_mix_of_masked_and_unmasked_paths_is_not_inside(self) -> None:
        directory = mask.MASK_PATHS[0]
        socket_path = next(path for path in mask.MASK_PATHS if path.endswith("docker.sock"))
        test_facts, _runner = facts(
            present=(directory, socket_path),
            directories=(directory,),
            mounts=(directory,),
            entries={directory: ()},
            devices={socket_path: 11},
        )
        observed = mask.observe(test_facts)
        self.assertIs(observed.state, mask.ProbeState.READY)
        self.assertEqual(observed.masked, (directory,))

    def test_socket_spellings_resolve_to_one_listed_path(self) -> None:
        socket_paths = [path for path in mask.MASK_PATHS if path.endswith("docker.sock")]
        self.assertEqual(len(socket_paths), 2)
        canonical = socket_paths[0]
        test_facts, _runner = facts(
            present=(canonical,),
            devices={canonical: 90},
            aliases={socket_paths[1]: canonical},
        )
        observed = mask.observe(test_facts)
        self.assertEqual(observed.present, (canonical,))
        self.assertIs(observed.state, mask.ProbeState.INSIDE)


class Wrapping(unittest.TestCase):
    """The wrapper keeps argv, identity, environment, and the drop contract."""

    def test_wrap_has_namespaces_stage_identity_and_command_positionals(self) -> None:
        caller = {
            name: f"ALLOW_{name}_SENTINEL" for name in mask.ENVIRONMENT_ALLOWLIST
        }
        caller["NOT_ALLOWED_NAME"] = "NOT_ALLOWED_SENTINEL"
        test_facts, _runner = facts(
            environment=caller,
            uid=1201,
            gid=1302,
            groups=(1302, 1403),
        )
        paths = (mask.MASK_PATHS[0],)
        command = ("pytest", "-x", "tests/test_mask.py")
        wrapped = mask.wrap(command, paths=paths, facts=test_facts)
        stage = wrapped.index(mask.ROOT_STAGE)
        self.assertEqual(wrapped[:2], ("sudo", "-n"))
        self.assertIn("--mount", wrapped[:stage])
        self.assertIn("--net", wrapped[:stage])
        self.assertEqual(wrapped[stage - 1], "-c")
        self.assertEqual(
            wrapped[stage + 1 : stage + 5], ("mask", "1201", "1302", "1302,1403")
        )
        self.assertIn("--reuid", mask.ROOT_STAGE)
        self.assertIn("--regid", mask.ROOT_STAGE)
        self.assertIn("--groups", mask.ROOT_STAGE)
        self.assertIn("--no-new-privs", mask.ROOT_STAGE)
        environment, listed_paths, tail = wrapped_parts(command, paths, test_facts)
        self.assertEqual(
            environment,
            tuple(f"{name}={caller[name]}" for name in mask.ENVIRONMENT_ALLOWLIST),
        )
        self.assertEqual(listed_paths, paths)
        self.assertEqual(tail, command)
        joined = " ".join(wrapped)
        self.assertNotIn("NOT_ALLOWED_SENTINEL", joined)
        self.assertNotIn("NOT_ALLOWED_NAME", joined)

    def test_missing_environment_names_stay_absent_and_identity_names_fallback(self) -> None:
        caller = {"PATH": "PATH_SENTINEL"}
        test_facts, _runner = facts(
            environment=caller,
            password_name="fallback-user",
            password_home="/home/fallback-user",
        )
        environment, _paths, _tail = wrapped_parts(("true",), (), test_facts)
        values = dict(item.split("=", 1) for item in environment)
        self.assertEqual(values["PATH"], "PATH_SENTINEL")
        self.assertEqual(values["HOME"], "/home/fallback-user")
        self.assertEqual(values["USER"], "fallback-user")
        self.assertEqual(values["LOGNAME"], "fallback-user")
        self.assertNotIn("LANG", values)
        self.assertNotEqual(values["HOME"], "/root")
        self.assertNotEqual(values["USER"], "root")
        self.assertNotEqual(values["LOGNAME"], "root")

    def test_probe_trial_uses_the_wrap_argv_and_true_as_command(self) -> None:
        test_facts, runner = facts(
            present=(mask.MASK_PATHS[0],),
            directories=(mask.MASK_PATHS[0],),
            mounts=(mask.MASK_PATHS[0],),
            entries={mask.MASK_PATHS[0]: ("visible",)},
            runner=RecordingRunner([0]),
        )
        result = mask.probe(("pytest", "-x"), test_facts)
        self.assertIs(result.state, mask.ProbeState.READY)
        self.assertEqual(len(runner.commands), 1)
        trial = runner.commands[0]
        expected = mask.wrap(("true",), paths=(mask.MASK_PATHS[0],), facts=test_facts)
        self.assertEqual(trial, expected)
        self.assertEqual(trial[-1], "true")
        self.assertIn("--mount", trial)
        self.assertIn("--net", trial)


class RootStage(unittest.TestCase):
    """The fixed shell text fails setup safely and preserves command codes."""

    def run_stage(
        self, *, failing: str | None, command: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "bin"
            tools.mkdir()
            masked = root / "masked"
            masked.mkdir()

            def write_tool(name: str, text: str) -> None:
                path = tools / name
                path.write_text(text, encoding="utf-8")
                path.chmod(0o755)

            mount_script = "#!/bin/sh\n"
            if failing == "rprivate":
                mount_script += 'if [ "$1" = "--make-rprivate" ]; then exit 1; fi\nexit 0\n'
            elif failing == "mount":
                mount_script += 'if [ "$1" = "--make-rprivate" ]; then exit 0; fi\nexit 1\n'
            else:
                mount_script += "exit 0\n"
            write_tool("mount", mount_script)
            write_tool("ip", "#!/bin/sh\n" + ("exit 1\n" if failing == "ip" else "exit 0\n"))
            if failing == "setpriv":
                setpriv_script = "#!/bin/sh\nexit 1\n"
            else:
                setpriv_script = """#!/bin/sh
while [ "$1" != env ]; do shift; done
shift
if [ "$1" = "-i" ]; then shift; fi
while [ "$#" -gt 0 ]; do
    case "$1" in
        *=*) export "$1"; shift ;;
        *) exec "$@" ;;
    esac
done
exit 0
"""
            write_tool("setpriv", setpriv_script)

            test_facts, _runner = facts(
                environment={"PATH": "/usr/bin"},
                runner=RecordingRunner(),
            )
            wrapped = mask.wrap(command, paths=(str(masked),), facts=test_facts)
            stage = wrapped.index(mask.ROOT_STAGE)
            stage_argv = (shell, "-c", mask.ROOT_STAGE, *wrapped[stage + 1 :])
            return subprocess.run(
                stage_argv,
                cwd=directory,
                env={"PATH": str(tools)},
                capture_output=True,
                text=True,
                check=False,
            )

    def test_each_setup_failure_is_code_71_and_cannot_fall_through(self) -> None:
        expected_steps = {"rprivate": "/", "mount": None, "ip": "loopback"}
        for failing, step in expected_steps.items():
            with self.subTest(failing=failing):
                result = self.run_stage(
                    failing=failing,
                    command=("sh", "-c", "exit 1"),
                )
                self.assertEqual(result.returncode, mask.SETUP_FAILURE_CODE)
                self.assertEqual(result.stdout, "")
                if step is None:
                    self.assertTrue(result.stderr.startswith("mask: setup failed at "))
                    self.assertTrue(result.stderr.endswith("/masked\n"))
                else:
                    self.assertEqual(result.stderr, f"mask: setup failed at {step}\n")

    def test_setpriv_trial_turns_exit_one_into_setup_failure(self) -> None:
        result = self.run_stage(failing="setpriv", command=("sh", "-c", "exit 1"))
        self.assertEqual(result.returncode, mask.SETUP_FAILURE_CODE)
        self.assertEqual(result.stderr, "mask: setup failed at drop\n")

    def test_successful_trial_preserves_the_command_exit_one(self) -> None:
        result = self.run_stage(failing=None, command=("sh", "-c", "exit 1"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")


class Main(unittest.TestCase):
    """The standalone command reports each observed state and returns its code."""

    def run_main(
        self, test_facts: mask.Facts, command: Sequence[str] = ("echo", "ok")
    ) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = mask.main(("--", *command), test_facts)
        return code, stdout.getvalue()

    def test_absent_host_runs_unmasked_and_returns_injected_codes(self) -> None:
        for expected in (71, 127):
            with self.subTest(expected=expected):
                runner = RecordingRunner([expected])
                test_facts, _runner = facts(runner=runner)
                code, output = self.run_main(test_facts, ("echo", "absent"))
                self.assertEqual(code, expected)
                self.assertEqual(output, "mask: no listed path is on this host\n")
                self.assertEqual(runner.commands, [("echo", "absent")])

    def test_inside_host_runs_as_stands(self) -> None:
        runner = RecordingRunner([17])
        test_facts, _runner = all_present_facts(runner=runner)
        code, output = self.run_main(test_facts, ("echo", "inside"))
        self.assertEqual(code, 17)
        self.assertEqual(output, "mask: already inside a mask\n")
        self.assertEqual(runner.commands, [("echo", "inside")])

    def test_ready_host_trials_then_runs_the_wrapped_command(self) -> None:
        runner = RecordingRunner([0, 23])
        test_facts, _runner = facts(
            present=(mask.MASK_PATHS[0],),
            directories=(mask.MASK_PATHS[0],),
            mounts=(mask.MASK_PATHS[0],),
            entries={mask.MASK_PATHS[0]: ("visible",)},
            runner=runner,
        )
        code, output = self.run_main(test_facts, ("echo", "ready"))
        self.assertEqual(code, 23)
        self.assertEqual(
            output,
            "mask: 1 listed paths hidden; network namespace isolated with loopback up\n",
        )
        self.assertEqual(len(runner.commands), 2)
        self.assertEqual(runner.commands[0][-1], "true")
        self.assertEqual(runner.commands[1][-2:], ("echo", "ready"))


class Imports(unittest.TestCase):
    """The path-runnable mask keeps the standard-library import boundary."""

    def test_mask_imports_only_standard_library_modules(self) -> None:
        path = ROOT / "tools" / "mask.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [node.module or ""]
            else:
                continue
            for target in targets:
                self.assertIn(
                    target.partition(".")[0],
                    sys.stdlib_module_names,
                    f"{path}:{node.lineno} imports {target}",
                )


if __name__ == "__main__":
    unittest.main()
