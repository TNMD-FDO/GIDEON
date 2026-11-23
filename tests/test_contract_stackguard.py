"""Hosted checks for the shared contract stack lock, clear, and wiring."""

import ast
import contextlib
import ctypes
import fcntl
import importlib.util
import io
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]

from gideon.host.cotenants import ContainerRow, is_marked

CONTRACT_DIR = Path(__file__).resolve().parent / "contract"
GUARD_PATH = CONTRACT_DIR / "stackguard.py"


def load_stackguard() -> Any:
    spec = importlib.util.spec_from_file_location("contract_stackguard", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STACKGUARD = load_stackguard()


class ProjectNames(unittest.TestCase):
    def test_every_compose_file_has_a_distinct_marked_project(self) -> None:
        files = STACKGUARD.COMPOSE_FILES
        self.assertCountEqual(files, CONTRACT_DIR.rglob("compose.yaml"))
        expected = tuple(yaml.safe_load(path.read_text())["name"] for path in files)
        names = STACKGUARD.project_names()
        self.assertEqual(names, expected)
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(is_marked(ContainerRow("example-container", name)))

    def test_unusable_compose_names_refuse_with_the_file(self) -> None:
        with TemporaryDirectory() as directory:
            first = Path(directory) / "first.yaml"
            second = Path(directory) / "second.yaml"
            first.write_text("name: example-contract\n")
            for detail, source in (
                ("missing file", None),
                ("invalid YAML", "name: ["),
                ("missing name", "services: {}\n"),
                ("duplicate name", first.read_text()),
            ):
                with self.subTest(detail=detail):
                    if source is not None:
                        second.write_text(source)
                    with self.assertRaises(STACKGUARD.StackGuardRefusal) as refusal:
                        STACKGUARD.project_names(compose_files=(first, second))
                    self.assertIn(str(second), str(refusal.exception))
                    self.assertIn("Fix:", str(refusal.exception))


def listing_commands(name: str) -> list[list[str]]:
    project = f"label=com.docker.compose.project={name}"
    return [
        ["docker", "ps", "-a", "-q", "--filter", project],
        ["docker", "network", "ls", "-q", "--filter", project],
        ["docker", "volume", "ls", "-q", "--filter", project],
    ]


def down_command(name: str) -> list[str]:
    return ["docker", "compose", "-p", name, "down", "-v", "--remove-orphans"]


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], tuple[int, ...]]] = []
        self.replies: dict[tuple[str, ...], Iterator[tuple[int, str, str]]] = {}

    def reply(self, argv: list[str], *results: tuple[int, str, str]) -> None:
        self.replies[tuple(argv)] = iter(results)

    def __call__(
        self, argv: list[str], *, pass_fds: tuple[int, ...] = ()
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv.copy(), pass_fds))
        result = next(self.replies.get(tuple(argv), iter(())), (0, "", ""))
        return subprocess.CompletedProcess(argv, *result)


class FakeClock:
    def __init__(self) -> None:
        self.now = 13.25
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class WithContractLock(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.lock_path = self.directory / "example.lock"
        self.stream = io.StringIO()
        self.enterContext(patch.dict(os.environ, {STACKGUARD.WAIT_VARIABLE: "0"}))
        os.environ.pop(STACKGUARD.PROJECT_VARIABLE, None)

    def take(self, runner: FakeDocker, **kwargs: Any) -> Any:
        handle = STACKGUARD.guard(runner=runner, lock_path=self.lock_path, stream=self.stream, **kwargs)
        self.addCleanup(handle.release)
        return handle

    @contextlib.contextmanager
    def hold_externally(self) -> Iterator[int]:
        self.lock_path.touch()
        with self.lock_path.open("rb") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield lock_file.fileno()


class ClearProjects(WithContractLock):
    def test_absent_projects_are_listed_once(self) -> None:
        runner = FakeDocker()
        self.take(runner)
        self.assertEqual(runner.calls, [
            (argv, ()) for name in STACKGUARD.project_names() for argv in listing_commands(name)
        ])
        self.assertEqual(len(self.stream.getvalue().splitlines()), 1)
        self.assertIn("neither project was present", self.stream.getvalue())

    def test_a_present_project_is_cleared_and_listed_again(self) -> None:
        names = STACKGUARD.project_names()
        present = names[-1]
        runner = FakeDocker()
        listings = listing_commands(present)
        runner.reply(listings[-1], (0, "example-volume\n", ""))
        handle = self.take(runner)
        expected: list[tuple[list[str], tuple[int, ...]]] = [
            (argv, ()) for name in names for argv in listing_commands(name)
        ]
        expected.append((down_command(present), (handle.fd,)))
        expected.extend((argv, ()) for argv in listings)
        self.assertEqual(runner.calls, expected)
        self.assertEqual(len(self.stream.getvalue().splitlines()), 1)
        self.assertIn(f"cleared {present}.", self.stream.getvalue())
        for name in names[:-1]:
            self.assertNotIn(f"cleared {name}", self.stream.getvalue())

    def test_listing_exit_down_exit_and_remainder_refuse_with_fixes(self) -> None:
        name = STACKGUARD.project_names()[0]
        listings = listing_commands(name)
        down = down_command(name)
        container = (0, "example-container\n", "")
        for stage, replies, fix, expected in (
            ("listing", [(listings[0], [(9, "example-id\n", "example listing failure")])],
             "use sudo", listings[:1]),
            ("down", [(listings[0], [container]), (down, [(8, "", "example down failure")])],
             STACKGUARD.HAND_COMMAND, [*listings, down]),
            ("remainder", [(listings[0], [container, container]),
                           (down, [(0, "", "example remainder report")])],
             STACKGUARD.HAND_COMMAND, [*listings, down, *listings]),
        ):
            with self.subTest(stage=stage):
                runner = FakeDocker()
                for argv, results in replies:
                    runner.reply(argv, *results)
                with self.assertRaises(STACKGUARD.StackGuardRefusal) as refusal:
                    self.take(runner)
                message = str(refusal.exception)
                self.assertIn(name, message)
                self.assertIn(f"example {stage}", message)
                self.assertIn("Fix:", message)
                self.assertIn(fix, message)
                self.assertNotIn("docker compose down", message)
                self.assertEqual([argv for argv, _ in runner.calls], expected)
                self.take(FakeDocker()).release()


class LockAcquisition(WithContractLock):
    def test_creation_under_private_umask_and_plain_reopen(self) -> None:
        old_umask = os.umask(0o077)
        try:
            handle = self.take(FakeDocker())
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(self.lock_path.stat().st_mode), 0o644)
        self.assertFalse(os.get_inheritable(handle.fd))
        self.assertEqual(fcntl.fcntl(handle.fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
        handle.release()
        with patch.object(os, "open", wraps=os.open) as open_file:
            self.take(FakeDocker()).release()
        self.assertEqual(open_file.call_count, 1)
        path, flags = open_file.call_args.args
        self.assertEqual(path, self.lock_path)
        self.assertEqual(flags & (os.O_ACCMODE | os.O_CREAT), os.O_RDONLY)
        self.assertTrue(flags & os.O_NOFOLLOW)

    def test_waiter_polls_then_refuses_with_the_real_holder(self) -> None:
        runner = FakeDocker()
        timer = FakeClock()
        poll = STACKGUARD.POLL_INTERVAL
        seconds = 2 * poll + 1
        with (
            self.hold_externally(),
            patch.dict(os.environ, {STACKGUARD.WAIT_VARIABLE: str(seconds)}),
            self.assertRaises(STACKGUARD.StackGuardRefusal) as refusal,
        ):
            self.take(runner, clock=timer.clock, sleep=timer.sleep)
        pid = os.getpid()
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        cmdline = " ".join(cmdline.replace("\0", " ").splitlines()).strip()
        self.assertTrue(cmdline)
        message = str(refusal.exception)
        for text in (str(self.lock_path), f"pid {pid} ({cmdline})", f"{seconds} seconds", "Fix:"):
            self.assertIn(text, message)
        self.assertIn(f"Let pid {pid} finish or stop it", message)
        waiting = self.stream.getvalue().splitlines()
        self.assertEqual(len(waiting), 1)
        self.assertIn(f"pid {pid} ({cmdline})", waiting[0])
        self.assertIn(f"{seconds} seconds", waiting[0])
        self.assertEqual(timer.sleeps, [poll, poll, 1])
        self.assertEqual(runner.calls, [])

    def test_release_during_poll_allows_the_final_attempt_to_clear(self) -> None:
        runner = FakeDocker()
        timer = FakeClock()
        poll = STACKGUARD.POLL_INTERVAL
        with self.hold_externally() as blocking_fd:
            def release_at_deadline(seconds: float) -> None:
                self.assertEqual(runner.calls, [])
                timer.sleep(seconds)
                fcntl.flock(blocking_fd, fcntl.LOCK_UN)

            with patch.dict(os.environ, {STACKGUARD.WAIT_VARIABLE: str(poll)}):
                self.take(runner, clock=timer.clock, sleep=release_at_deadline)
        self.assertEqual(timer.sleeps, [poll])
        self.assertEqual(len(runner.calls), sum(len(listing_commands(name)) for name in STACKGUARD.project_names()))
        waiting, acquired = self.stream.getvalue().splitlines()
        self.assertIn("Waiting", waiting)
        self.assertIn("taken", acquired)

    def test_wait_variable_accepts_whole_non_negative_seconds(self) -> None:
        with patch.dict(os.environ):
            del os.environ[STACKGUARD.WAIT_VARIABLE]
            self.assertEqual(STACKGUARD.lock_wait(), STACKGUARD.DEFAULT_WAIT)
            for seconds in (0, 17):
                with self.subTest(seconds=seconds):
                    os.environ[STACKGUARD.WAIT_VARIABLE] = str(seconds)
                    self.assertEqual(STACKGUARD.lock_wait(), seconds)
            for value in ("example-seconds", "-1", "1.5", ""):
                with self.subTest(value=value):
                    os.environ[STACKGUARD.WAIT_VARIABLE] = value
                    with self.assertRaises(STACKGUARD.StackGuardRefusal) as refusal:
                        STACKGUARD.lock_wait()
                    self.assertIn(STACKGUARD.WAIT_VARIABLE, str(refusal.exception))

    def test_a_project_name_override_refuses_before_the_lock_or_any_listing(self) -> None:
        runner = FakeDocker()
        with (
            patch.dict(os.environ, {STACKGUARD.PROJECT_VARIABLE: "example-override"}),
            self.assertRaises(STACKGUARD.StackGuardRefusal) as refusal,
        ):
            self.take(runner)
        message = str(refusal.exception)
        for text in (STACKGUARD.PROJECT_VARIABLE, "example-override", "Fix: Unset"):
            self.assertIn(text, message)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.lock_path.exists())

    def test_second_take_refuses_immediately_and_release_is_idempotent(self) -> None:
        handle = self.take(FakeDocker())
        timer = FakeClock()
        runner = FakeDocker()
        with (
            patch.dict(os.environ, {STACKGUARD.WAIT_VARIABLE: str(STACKGUARD.DEFAULT_WAIT)}),
            self.assertRaises(STACKGUARD.StackGuardRefusal) as refusal,
        ):
            self.take(runner, clock=timer.clock, sleep=timer.sleep)
        self.assertIn("This process already holds", str(refusal.exception))
        self.assertEqual(timer.sleeps, [])
        self.assertEqual(runner.calls, [])
        handle.release()
        next_handle = self.take(runner)
        handle.release()
        self.assertEqual(os.fstat(next_handle.fd).st_ino, self.lock_path.stat().st_ino)


class HolderWording(WithContractLock):
    def test_recorded_table_distinguishes_live_exited_and_unknown_holders(self) -> None:
        pid = os.getpid()
        exited_pid = 2**31 - 1
        self.assertFalse(Path(f"/proc/{exited_pid}").exists())
        table = self.directory / "locks.txt"
        unreadable = self.directory / "unreadable-locks"
        unreadable.mkdir()
        with self.hold_externally():
            info = self.lock_path.stat()
            device = f"{os.major(info.st_dev):x}:{os.minor(info.st_dev):x}"
            key = f"{device}:{info.st_ino}"
            other_rows = (
                f"1: -> FLOCK ADVISORY WRITE {exited_pid} {key} 0 EOF\n"
                f"2: POSIX ADVISORY WRITE {exited_pid} {key} 0 EOF\n"
                f"3: FLOCK ADVISORY WRITE {exited_pid} {device}:{info.st_ino + 1} 0 EOF\n"
                f"4: FLOCK ADVISORY WRITE {exited_pid} {device}:example 0 EOF\n"
            )
            for detail, holder, read_cmdline, expected in (
                ("live", pid, True, f"pid {pid} ("),
                ("live without cmdline", pid, False, f"held by pid {pid}."),
                ("exited", exited_pid, True, f"(pid {exited_pid}) has exited"),
                ("unmatched", None, True, "holder could not be identified"),
                ("unreadable", None, True, "holder could not be identified"),
            ):
                with self.subTest(detail=detail), contextlib.ExitStack() as stack:
                    row = f"5: FLOCK ADVISORY WRITE {holder} {key} 0 EOF\n" if holder else ""
                    table.write_text(other_rows + row)
                    if not read_cmdline:
                        stack.enter_context(patch.object(Path, "read_bytes", side_effect=PermissionError))
                    runner = FakeDocker()
                    with self.assertRaises(STACKGUARD.StackGuardRefusal) as refusal:
                        self.take(runner, table_path=unreadable if detail == "unreadable" else table)
                    message = str(refusal.exception)
                    self.assertIn(expected, message)
                    if holder == pid:
                        self.assertIn(f"Let pid {pid} finish or stop it", message)
                    else:
                        self.assertIn(f"sudo fuser -v {self.lock_path}", message)
                        if holder == exited_pid:
                            self.assertIn("something it started still holds it", message)
                    self.assertEqual(runner.calls, [])


class HandEntry(WithContractLock):
    def test_hand_clear_prints_one_line_on_stderr_and_releases(self) -> None:
        name = STACKGUARD.project_names()[0]
        runner = FakeDocker()
        runner.reply(listing_commands(name)[0], (0, "example-container\n", ""))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(STACKGUARD.main(runner=runner, lock_path=self.lock_path), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        self.assertIn(f"cleared {name}", stderr.getvalue())
        self.take(FakeDocker())

    def test_hand_refusal_prints_one_line_on_stderr_and_returns_one(self) -> None:
        name = STACKGUARD.project_names()[0]
        runner = FakeDocker()
        runner.reply(listing_commands(name)[0], (6, "", "example Docker access error\nexample detail"))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(STACKGUARD.main(runner=runner, lock_path=self.lock_path), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        for text in (name, "example Docker access error example detail", "Fix:", "sudo"):
            self.assertIn(text, stderr.getvalue())
        self.take(FakeDocker())

    def test_hand_entry_also_reports_its_first_failed_take(self) -> None:
        runner = FakeDocker()
        with self.hold_externally():
            self.assertEqual(STACKGUARD.main(runner=runner, lock_path=self.lock_path, stream=self.stream), 1)
        waiting, refusal = self.stream.getvalue().splitlines()
        self.assertIn("Waiting", waiting)
        self.assertIn("Fix:", refusal)
        self.assertEqual(runner.calls, [])


# Adopt the sleeper after the producer exits so the test can reap it itself.
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
PROCESS_TIMEOUT = 3


@contextlib.contextmanager
def adopt_orphans() -> Iterator[None]:
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    previous = ctypes.c_int()
    if prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot read the child subreaper setting")
    if prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot adopt the orphaned lock-holder child")
    try:
        yield
    finally:
        if prctl(PR_SET_CHILD_SUBREAPER, previous.value, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot restore the child subreaper setting")


PRODUCER = textwrap.dedent("""\
    import os
    import subprocess
    import sys
    from pathlib import Path
    from runpy import run_path

    guard_path, lock_path, inherit, log_path = sys.argv[1:]
    guard = run_path(guard_path)["guard"]
    def fake_docker(argv):
        return subprocess.CompletedProcess(argv, 0, "", "")

    handle = guard(runner=fake_docker, lock_path=Path(lock_path))
    pass_fds = (handle.fd,) if inherit == "yes" else ()
    with Path(log_path).open("w") as log:
        # Sleep on the pipe until the test closes its write end.
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import sys; sys.stdin.buffer.read(1)"],
            pass_fds=pass_fds, stdout=log, stderr=log,
        )
    print(child.pid, flush=True)
    os._exit(0)
    """)


def reap_group(group: int) -> dict[int, int]:
    exits: dict[int, int] = {}
    deadline = time.monotonic() + PROCESS_TIMEOUT
    while True:
        try:
            pid, status = os.waitpid(-group, os.WNOHANG)
        except ChildProcessError:
            return exits
        if pid:
            exits[pid] = os.waitstatus_to_exitcode(status)
        elif time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out reaping children of process group {group}")
        else:
            time.sleep(0.01)


class InheritedLock(WithContractLock):
    def test_surviving_child_holds_only_the_descriptor_it_inherits(self) -> None:
        for inherit in (True, False):
            with self.subTest(inherit=inherit), adopt_orphans():
                read_fd, write_fd = os.pipe()
                with os.fdopen(read_fd, "rb") as sleeper_stdin, os.fdopen(write_fd, "wb") as wake:
                    producer = subprocess.Popen(
                        [sys.executable, "-B", "-c", PRODUCER, str(GUARD_PATH), str(self.lock_path),
                         "yes" if inherit else "no", str(self.directory / "sleeper.log")],
                        stdin=sleeper_stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, start_new_session=True,
                    )
                    try:
                        stdout, stderr = producer.communicate(timeout=PROCESS_TIMEOUT)
                        self.assertEqual(producer.returncode, 0, stderr)
                        sleeper_pid = int(stdout)
                        self.assertIsNone(os.waitid(os.P_PID, sleeper_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))
                        runner = FakeDocker()
                        if inherit:
                            with self.assertRaises(STACKGUARD.StackGuardRefusal):
                                self.take(runner)
                            self.assertEqual(runner.calls, [])
                        else:
                            self.take(runner).release()
                            self.assertTrue(runner.calls)
                    finally:
                        wake.close()
                        if producer.poll() is None:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(producer.pid, signal.SIGKILL)
                            producer.communicate(timeout=PROCESS_TIMEOUT)
                        try:
                            exits = reap_group(producer.pid)
                        except TimeoutError:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(producer.pid, signal.SIGKILL)
                            reap_group(producer.pid)
                            raise
                self.assertEqual(exits, {sleeper_pid: 0})
                self.take(FakeDocker()).release()


def calls_to(node: ast.AST, *names: str) -> list[ast.Call]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Call) and ast.unparse(child.func) in names]


def releases_after_down(teardown: ast.FunctionDef) -> bool:
    downs = calls_to(teardown, "stack.down", "cls.stack.down")
    releases = calls_to(teardown, "cls.guard.release")
    if len(downs) != 1 or len(releases) != 1:
        return False
    return any(
        isinstance(node, ast.Try)
        and any(downs[0] in ast.walk(stmt) for stmt in node.body)
        and any(releases[0] in ast.walk(stmt) for stmt in node.finalbody)
        for node in ast.walk(teardown)
    )


def wiring_findings(source: str) -> set[str]:
    methods = {
        node.name: node for definition in ast.parse(source).body if isinstance(definition, ast.ClassDef)
        for node in definition.body if isinstance(node, ast.FunctionDef)
    }
    findings = set()
    setup = methods.get("setUpClass")
    if setup is None or ast.unparse(setup.body[0]) != "cls.guard = stackguard.guard()":
        findings.add("take the guard first in setUpClass")
    teardown = methods.get("tearDownClass")
    if teardown is None or not releases_after_down(teardown):
        findings.add("release the guard in finally after stack.down()")
    compose = methods.get("compose")
    calls = [] if compose is None else calls_to(compose, "subprocess.run")
    if not calls or not all(
        any(keyword.arg == "pass_fds" and ast.unparse(keyword.value) == "(self.guard.fd,)"
            for keyword in call.keywords)
        for call in calls
    ):
        findings.add("pass the descriptor to each Compose child")
    return findings


class ContractWiring(unittest.TestCase):
    def test_both_contracts_hold_the_guard_through_compose_teardown(self) -> None:
        for filename in ("owui_apply_manifest.py", "search_sentinel.py"):
            with self.subTest(filename=filename):
                self.assertEqual(wiring_findings((CONTRACT_DIR / filename).read_text()), set())

    def test_each_wiring_check_rejects_a_seeded_source(self) -> None:
        source = textwrap.dedent("""\
            class ExampleStack:
                def compose(self, *args):
                    return subprocess.run(args, pass_fds=(self.guard.fd,))

            class ExampleContract:
                def setUpClass(cls):
                    cls.guard = stackguard.guard()
                    cls.stack.compose("up")

                def tearDownClass(cls):
                    try:
                        cls.stack.down()
                    finally:
                        cls.guard.release()
            """)
        self.assertEqual(wiring_findings(source), set())
        for original, changed, finding in (
            ('cls.guard = stackguard.guard()\n        cls.stack.compose("up")',
             'cls.stack.compose("up")\n        cls.guard = stackguard.guard()',
             "take the guard first in setUpClass"),
            ("try:\n            cls.stack.down()\n        finally:\n            cls.guard.release()",
             "cls.stack.down()\n        cls.guard.release()", "release the guard in finally after stack.down()"),
            (", pass_fds=(self.guard.fd,)", "", "pass the descriptor to each Compose child"),
        ):
            with self.subTest(finding=finding):
                self.assertEqual(wiring_findings(source.replace(original, changed)), {finding})
