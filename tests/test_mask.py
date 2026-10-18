"""Contract tests for the standard-library mask and its fixed root stage."""

from __future__ import annotations

import ast
import contextlib
import io
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from gideon.host.steps.services import MASK_SETUP_FAILURE_CODE, mask_wrapper_text
from tools import mask

ROOT = Path(__file__).resolve().parent.parent


class RecordingRunner:
    """Record injected commands and return their scripted exit codes."""

    def __init__(self, codes: Sequence[int] = ()) -> None:
        self.codes = list(codes)
        self.commands: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, command: Sequence[str], stdin: str | None) -> int:
        self.commands.append((tuple(command), stdin))
        return self.codes[len(self.commands) - 1] if self.codes else 0


def facts(
    *,
    present: Sequence[str] = (),
    directories: Sequence[str] = (),
    mounts: Sequence[str] = (),
    entries: Mapping[str, Sequence[str]] | None = None,
    null_device: int = 90,
    devices: Mapping[str, int] | None = None,
    wrapper_present: bool = True,
    uid: int = 1000,
    environment: Mapping[str, str] | None = None,
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

    def device(path: str) -> int:
        return device_map.get(realpath(path), null_device if path == "/dev/null" else 10)

    def which(tool: str) -> str | None:
        if tool in missing:
            return None
        return executable_map.get(tool, f"/usr/bin/{tool}")

    return (
        mask.Facts(
            exists=lambda path: (path == mask.WRAPPER_PATH and wrapper_present) or realpath(path) in canonical_present,
            is_directory=lambda path: realpath(path) in canonical_directories,
            is_mount=lambda path: realpath(path) in canonical_mounts,
            list_directory=lambda path: canonical_entries.get(realpath(path), ()),
            device=device,
            realpath=realpath,
            effective_uid=lambda: uid,
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


class Refusals(unittest.TestCase):
    """Probe refusals are ordered and carry separate fixes."""

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
        wrapper_present: bool = True,
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
            wrapper_present=wrapper_present,
            uid=uid,
            missing_tools=missing_tools,
            environment=environment,
            executable_paths=executable_paths,
            working_directory=working_directory,
            runner=runner,
        )

    def test_root_refusal_precedes_tool_checks(self) -> None:
        test_facts, runner = self.ready_facts(uid=0, missing_tools=mask.REQUIRED_TOOLS, wrapper_present=False)
        refusal = self.assert_refused(mask.probe(("pytest",), test_facts))
        self.assertIn("root", refusal.problem)
        self.assertEqual(runner.commands, [])

    def test_missing_tool_refusal_precedes_path_and_trial_checks(self) -> None:
        test_facts, runner = self.ready_facts(
            missing_tools=(mask.REQUIRED_TOOLS[0],),
            wrapper_present=False,
            working_directory=mask.MASK_PATHS[0],
        )
        refusal = self.assert_refused(mask.probe(("pytest",), test_facts))
        self.assertIn(mask.REQUIRED_TOOLS[0], refusal.problem)
        self.assertEqual(runner.commands, [])

    def test_absent_wrapper_refusal_precedes_newline_path_and_trial_checks(self) -> None:
        test_facts, runner = self.ready_facts(
            wrapper_present=False, working_directory=mask.MASK_PATHS[0],
        )
        refusal = self.assert_refused(mask.probe(("pytest", "fictitious\nword"), test_facts))
        self.assertEqual(refusal.problem, f"the mask wrapper {mask.WRAPPER_PATH} is absent")
        self.assertEqual(
            refusal.fix,
            "Run sudo python3 -m gideon host provision --only gh-runner from a checkout at main, then re-run.",
        )
        self.assertEqual(runner.commands, [])

    def test_newline_word_refusal_precedes_path_and_trial_checks(self) -> None:
        for command in (("fictitious\ncommand",), ("pytest", "fictitious\nword")):
            with self.subTest(command=command):
                test_facts, runner = self.ready_facts(working_directory=mask.MASK_PATHS[0])
                refusal = self.assert_refused(mask.probe(command, test_facts))
                self.assertIn("newline", refusal.problem)
                self.assertIn("newline", refusal.fix)
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
    """Only the fixed wrapper is in argv; command words travel on stdin."""

    def test_wrap_has_the_fixed_argv_and_one_word_per_line(self) -> None:
        test_facts, _runner = facts(environment={"HOME": "/fictitious/caller"})
        command = ("pytest", "-x", "a word with spaces", "", "$(fictitious-command)")
        wrapped = mask.wrap(command, paths=(mask.MASK_PATHS[0],), facts=test_facts)
        self.assertEqual(wrapped.argv, ("sudo", "-n", mask.WRAPPER_PATH))
        self.assertEqual(wrapped.stdin, "pytest\n-x\na word with spaces\n\n$(fictitious-command)\n")

    def test_paths_are_owned_by_the_wrapper_and_empty_command_has_empty_stdin(self) -> None:
        wrapped = mask.wrap((), paths=("/fictitious/path",))
        self.assertEqual(wrapped.argv, ("sudo", "-n", mask.WRAPPER_PATH))
        self.assertEqual(wrapped.stdin, "")

    def test_probe_trial_sends_true_to_the_fixed_wrapper(self) -> None:
        test_facts, runner = facts(
            present=(mask.MASK_PATHS[0],),
            directories=(mask.MASK_PATHS[0],),
            mounts=(mask.MASK_PATHS[0],),
            entries={mask.MASK_PATHS[0]: ("visible",)},
            runner=RecordingRunner([0]),
        )
        result = mask.probe(("pytest", "-x"), test_facts)
        self.assertIs(result.state, mask.ProbeState.READY)
        self.assertEqual(runner.commands, [(("sudo", "-n", mask.WRAPPER_PATH), "true\n")])


class RootStage(unittest.TestCase):
    """The provision-owned shell text fails setup safely and preserves command codes."""

    def run_stage(
        self,
        *,
        failing: str | None = None,
        command: Sequence[str] = ("true",),
        arguments: Sequence[str] = (),
        include_sudo_uid: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], list[tuple[str, ...]], tuple[str, ...]]:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "bin"
            tools.mkdir()
            masked = root / "masked directory's path"
            masked.mkdir()
            masked_file = root / "masked file"
            masked_file.touch()
            paths = (str(masked), str(masked_file), str(root / "missing"))
            log = root / "operations"
            log.touch()

            def write_tool(name: str, text: str) -> None:
                path = tools / name
                path.write_text("#!/bin/sh\n" + text, encoding="utf-8")
                path.chmod(0o755)

            write_tool(
                "unshare",
                f'printf "unshare\\t%s\\t%s\\n" "$1" "$2" >> {shlex.quote(str(log))}\n'
                '[ "$1" = --mount ] && [ "$2" = --net ] && [ "$3" = -- ] || exit 1\n'
                + ("exit 1\n" if failing == "namespace" else 'shift 3\nexec "$@"\n'),
            )
            mount_script = (
                '{ printf "mount"; printf "\\t%s" "$@"; printf "\\n"; } >> '
                f"{shlex.quote(str(log))}\n"
            )
            if failing == "rprivate":
                mount_script += '[ "$1" != --make-rprivate ]\n'
            elif failing == "mount":
                mount_script += '[ "$1" = --make-rprivate ]\n'
            else:
                mount_script += "exit 0\n"
            write_tool("mount", mount_script)
            write_tool("ip", "exit 1\n" if failing == "loopback" else "exit 0\n")
            write_tool(
                "setpriv",
                "exit 1\n" if failing == "drop" else
                'while [ "$1" != env ]; do shift; done\nexec "$@"\n',
            )
            wrapper = root / "wrapper"
            wrapper.write_text(mask_wrapper_text(paths=paths), encoding="utf-8")
            environment = {
                "PATH": f"{tools}:/usr/bin:/bin",
                "SUDO_GID": str(os.getgid()),
                "HOME": "/fictitious/inherited/home",
                "USER": "fictitious-inherited-user",
                "LOGNAME": "fictitious-inherited-logname",
                "LANG": "fictitious-inherited-lang",
                "NOT_ALLOWED": "fictitious-inherited-value",
            }
            if include_sudo_uid:
                environment["SUDO_UID"] = str(os.getuid())
            result = subprocess.run(
                (shell, str(wrapper), *arguments),
                input=mask.wrap(command).stdin,
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            operations = [tuple(line.split("\t")) for line in log.read_text().splitlines()]
            return result, operations, paths

    def assert_setup_failure(self, result: subprocess.CompletedProcess[str], step: str) -> None:
        self.assertEqual(result.returncode, MASK_SETUP_FAILURE_CODE)
        self.assertEqual(result.stdout, "")
        self.assertRegex(
            result.stderr,
            rf"^mask: setup failed at {re.escape(step)}\. Fix: [^\n]+\n$",
        )

    def test_each_setup_failure_has_its_one_line_and_cannot_fall_through(self) -> None:
        for failing in ("namespace", "rprivate", "mount", "loopback", "drop"):
            with self.subTest(failing=failing):
                result, operations, paths = self.run_stage(failing=failing, command=("false",))
                self.assert_setup_failure(result, paths[0] if failing == "mount" else failing)
                if failing == "namespace":
                    self.assertFalse(any(operation[0] == "mount" for operation in operations))

    def test_namespaces_precede_mounts_and_only_present_paths_are_masked(self) -> None:
        result, operations, paths = self.run_stage()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(operations[:2], [("unshare", "--mount", "--net")] * 2)
        self.assertEqual(
            operations[2:],
            [
                ("mount", "--make-rprivate", "/"),
                ("mount", "-t", "tmpfs", "-o", "ro", "tmpfs", paths[0]),
                ("mount", "--bind", "/dev/null", paths[1]),
            ],
        )

    def test_arguments_missing_uid_and_empty_stdin_are_refused(self) -> None:
        result, operations, _paths = self.run_stage(arguments=("extra",))
        self.assert_setup_failure(result, "arguments")
        self.assertIn("no argument", result.stderr)
        self.assertEqual(operations, [])
        result, operations, _paths = self.run_stage(include_sudo_uid=False)
        self.assert_setup_failure(result, "identity")
        self.assertIn("through sudo", result.stderr)
        self.assertEqual(operations, [])
        result, _operations, _paths = self.run_stage(command=())
        self.assert_setup_failure(result, "command")
        self.assertIn("on stdin", result.stderr)

    def test_successful_trial_preserves_the_command_exit_one(self) -> None:
        result, _operations, _paths = self.run_stage(command=("false",))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")

    def test_command_sees_only_the_fixed_environment_with_account_values(self) -> None:
        result, _operations, _paths = self.run_stage(command=("/usr/bin/env",))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        environment = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual(set(environment), {"HOME", "PATH", "USER", "LOGNAME", "LANG"})
        account = pwd.getpwuid(os.getuid())
        self.assertEqual(environment["HOME"], account.pw_dir)
        self.assertEqual(environment["USER"], account.pw_name)
        self.assertEqual(environment["LOGNAME"], account.pw_name)
        self.assertEqual(environment["LANG"], "C.UTF-8")
        self.assertIn("/usr/bin", environment["PATH"].split(":"))

    def test_command_words_are_literal_and_command_stdin_is_empty(self) -> None:
        words = ("", "a word with spaces", "$(printf expanded)", "`printf expanded`", '"quoted"')
        result, _operations, _paths = self.run_stage(command=("printf", "<%s>\\n", *words))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "".join(f"<{word}>\n" for word in words))
        self.assertEqual(result.stderr, "")
        result, _operations, _paths = self.run_stage(
            command=("sh", "-c", "if read -r word; then exit 1; fi"),
        )
        self.assertEqual(result.returncode, 0)
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
                self.assertEqual(runner.commands, [(("echo", "absent"), None)])

    def test_inside_host_runs_as_stands(self) -> None:
        runner = RecordingRunner([17])
        test_facts, _runner = all_present_facts(runner=runner)
        code, output = self.run_main(test_facts, ("echo", "inside"))
        self.assertEqual(code, 17)
        self.assertEqual(output, "mask: already inside a mask\n")
        self.assertEqual(runner.commands, [(("echo", "inside"), None)])

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
        self.assertEqual(runner.commands[0], (("sudo", "-n", mask.WRAPPER_PATH), "true\n"))
        self.assertEqual(runner.commands[1], (("sudo", "-n", mask.WRAPPER_PATH), "echo\nready\n"))


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
