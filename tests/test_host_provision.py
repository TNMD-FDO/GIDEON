"""Runner contract tests using an in-process Host and fake steps."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

from gideon.host.nogpu import (
    BUILD_BOX_ONLY_FIX,
    BUILD_BOX_PATH,
    NO_GPU_DECLARED_PROBLEM,
    NO_GPU_PATH,
    NOT_BUILD_BOX_DETAIL,
)
from gideon.host.provision import run_provision
from gideon.host.steps import (
    CheckResult,
    Disposition,
    ProvisionContext,
    Step,
    StepFailure,
)
from gideon.host.sysio import Command, PathLike, RealHost


class RealHostRun(unittest.TestCase):
    def test_absent_binary_degrades_to_127_instead_of_raising(self) -> None:
        result = RealHost().run(["gideon-no-such-binary"])
        self.assertEqual(result.returncode, 127)
        self.assertIn("command not found", result.stderr)
        with self.assertRaises(subprocess.CalledProcessError):
            RealHost().run(["gideon-no-such-binary"], check=True)

    def test_a_child_never_reads_the_terminal(self) -> None:
        # stdin is /dev/null unless input is given: a docker compose exec or a
        # nested sudo under a pty must find no terminal to read or to juggle.
        probe = [sys.executable, "-c", "import sys; sys.stdout.write(repr(sys.stdin.read()))"]
        self.assertEqual(RealHost().run(probe).stdout, "''")
        self.assertEqual(RealHost().run(probe, input="fed").stdout, "'fed'")
        streamed = RealHost().run(
            [sys.executable, "-c", "import sys; sys.exit(0 if sys.stdin.read() == '' else 3)"],
            passthrough=True,
        )
        self.assertEqual(streamed.returncode, 0)

    def test_passthrough_inherits_the_streams_and_keeps_the_127_contract(self) -> None:
        # The child writes to this process's descriptor 1, so the seam captures
        # nothing; a temporary file stands in for the operator's terminal.
        with tempfile.TemporaryFile("w+") as terminal:
            saved = os.dup(1)
            try:
                os.dup2(terminal.fileno(), 1)
                result = RealHost().run(
                    [sys.executable, "-c", "print('streamed')"], passthrough=True
                )
            finally:
                os.dup2(saved, 1)
                os.close(saved)
            terminal.seek(0)
            self.assertEqual(terminal.read(), "streamed\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        absent = RealHost().run(["gideon-no-such-binary"], passthrough=True)
        self.assertEqual(absent.returncode, 127)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = REPO_ROOT / "host.lock"


class FakeHost:
    """A root-looking Host that reads only explicitly supplied fixture files."""

    def __init__(self, *, files: dict[str, str] | None = None, euid: int = 0) -> None:
        self.files = files or {}
        self.euid = euid
        self.calls: list[tuple[str, object]] = []

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key in self.files:
            return self.files[key]
        return Path(key).read_text()

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding, mode
        key = os.fspath(path)
        self.calls.append(("write_text", (key, text)))
        self.files[key] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files or Path(path).exists()

    def listdir(self, path: PathLike) -> list[str]:
        root = Path(path)
        entries: set[str] = set()
        for name in self.files:
            candidate = Path(name)
            if candidate.parent == root:
                entries.add(candidate.name)
            elif candidate.parent.parent == root:
                entries.add(candidate.parent.name)
        return sorted(entries)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del path, missing_ok

    def stat(self, path: PathLike) -> os.stat_result:
        return os.stat(path)

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

    def geteuid(self) -> int:
        return self.euid


class ScriptStep(Step):
    def __init__(
        self,
        name: str,
        results: Sequence[CheckResult],
        *,
        requires: tuple[str, ...] = (),
        needs_site: bool = False,
        gpu_host_only: bool = False,
        build_box_only: bool = False,
        announcement: str | None = None,
    ) -> None:
        self.name = name
        self.summary = f"summary for {name}"
        self.requires = requires
        self.needs_site = needs_site
        self.gpu_host_only = gpu_host_only
        self.build_box_only = build_box_only
        self.results = list(results)
        self.check_calls = 0
        self.apply_calls = 0
        self.announcement = announcement

    def check(self, context: ProvisionContext) -> CheckResult:
        del context
        self.check_calls += 1
        return self.results.pop(0) if self.results else CheckResult(
            Disposition.CONVERGED, "converged", ""
        )

    def apply(self, context: ProvisionContext) -> str | None:
        del context
        self.apply_calls += 1
        return self.announcement


class RaisingStep(ScriptStep):
    def __init__(self, error: Exception) -> None:
        super().__init__("raising", [result(Disposition.DRIFT)])
        self.error = error

    def apply(self, context: ProvisionContext) -> str | None:
        del context
        raise self.error


def result(
    disposition: Disposition,
    *,
    fix: str = "fix it",
    halts_run: bool = False,
) -> CheckResult:
    return CheckResult(disposition, disposition.value, fix, halts_run)


def arguments(
    *,
    only: str | None = None,
    dry_run: bool = False,
    listing: bool = False,
    build_box: bool = False,
):
    return type(
        "Arguments",
        (),
        {
            "only": only,
            "dry_run": dry_run,
            "list": listing,
            "no_gpu": False,
            "build_box": build_box,
        },
    )()


class RunnerTests(unittest.TestCase):
    def run_steps(
        self,
        steps: Sequence[Step],
        *,
        args: object | None = None,
        host: FakeHost | None = None,
        site_path: PathLike = "/missing/site.yaml",
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_provision(
                args or arguments(),
                host=host or FakeHost(),
                lock_path=LOCK_PATH,
                site_path=site_path,
                steps=steps,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_applies_only_drift_and_rechecks(self) -> None:
        step = ScriptStep("drift", [result(Disposition.DRIFT), result(Disposition.CONVERGED)])
        code, output, _ = self.run_steps([step])
        self.assertEqual(code, 0)
        self.assertEqual(step.apply_calls, 1)
        self.assertEqual(step.check_calls, 2)
        self.assertIn("drift: applied", output)

    def test_converged_does_not_apply_and_dry_run_would_apply(self) -> None:
        converged = ScriptStep("ready", [result(Disposition.CONVERGED)])
        self.assertEqual(self.run_steps([converged])[0], 0)
        self.assertEqual(converged.apply_calls, 0)

        dry = ScriptStep("dry", [result(Disposition.DRIFT)])
        code, output, _ = self.run_steps([dry], args=arguments(dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(dry.apply_calls, 0)
        self.assertEqual(dry.check_calls, 1)
        self.assertIn("dry: would-apply", output)

    def test_apply_announcement_is_printed_once_after_its_row(self) -> None:
        step = ScriptStep(
            "announced",
            [result(Disposition.DRIFT), result(Disposition.CONVERGED)],
            announcement="store this identity in the office password manager now",
        )
        code, output, _ = self.run_steps([step])
        self.assertEqual(code, 0)
        self.assertEqual(
            output.count("store this identity in the office password manager now"),
            1,
        )
        self.assertLess(output.index("announced: applied"), output.index("store this identity"))

        dry = ScriptStep(
            "dry-announced",
            [result(Disposition.DRIFT)],
            announcement="should not print",
        )
        code, output, _ = self.run_steps([dry], args=arguments(dry_run=True))
        self.assertEqual(code, 0)
        self.assertNotIn("should not print", output)

    def test_dispositions_map_to_expected_outcomes(self) -> None:
        reboot = ScriptStep("reboot", [result(Disposition.REBOOT_REQUIRED)])
        blocked = ScriptStep("blocked", [result(Disposition.PENDING_INPUT)])
        failed = ScriptStep("failed", [result(Disposition.UNFIXABLE)])
        code, output, _ = self.run_steps([reboot, blocked, failed])
        self.assertEqual(code, 1)
        self.assertIn("reboot: reboot-required", output)
        self.assertIn("blocked: blocked", output)
        self.assertIn("failed: failed", output)

    def test_apply_that_does_not_converge_fails(self) -> None:
        step = ScriptStep("stuck", [result(Disposition.DRIFT), result(Disposition.DRIFT)])
        code, output, _ = self.run_steps([step])
        self.assertEqual(code, 1)
        self.assertIn("stuck: failed", output)

    def test_step_failure_renders_its_detail_and_fix(self) -> None:
        step = RaisingStep(StepFailure("job is still running", "Wait, then re-run provision."))
        code, output, _ = self.run_steps([step])
        self.assertEqual(code, 1)
        self.assertIn("raising: failed", output)
        self.assertIn("job is still running", output)
        self.assertIn("Wait, then re-run provision.", output)

    def test_plain_apply_exception_keeps_generic_fix(self) -> None:
        step = RaisingStep(RuntimeError("unexpected"))
        code, output, _ = self.run_steps([step])
        self.assertEqual(code, 1)
        self.assertIn("apply raised RuntimeError: unexpected", output)
        self.assertIn("Repair the raising apply failure and re-run provision.", output)

    def test_halt_and_failed_prerequisite_skip_dependents(self) -> None:
        halted = ScriptStep("halt", [result(Disposition.UNFIXABLE, halts_run=True)])
        dependent = ScriptStep("dependent", [result(Disposition.CONVERGED)], requires=("halt",))
        code, output, _ = self.run_steps([halted, dependent])
        self.assertEqual(code, 1)
        self.assertEqual(dependent.check_calls, 0)
        self.assertIn("dependent: skipped", output)

        failed = ScriptStep("failed", [result(Disposition.UNFIXABLE)])
        sibling = ScriptStep("sibling", [result(Disposition.CONVERGED)])
        dependent = ScriptStep("dependent", [result(Disposition.CONVERGED)], requires=("failed",))
        code, output, _ = self.run_steps([failed, sibling, dependent])
        self.assertEqual(code, 1)
        self.assertEqual(sibling.check_calls, 1)
        self.assertEqual(dependent.check_calls, 0)
        self.assertIn("dependent: skipped", output)

    def test_only_refuses_unconverged_prerequisite_and_does_not_apply_it(self) -> None:
        prerequisite = ScriptStep("first", [result(Disposition.DRIFT)])
        target = ScriptStep("second", [result(Disposition.CONVERGED)], requires=("first",))
        code, output, error = self.run_steps([prerequisite, target], args=arguments(only="second"))
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertEqual(prerequisite.apply_calls, 0)
        self.assertIn("first", error)
        self.assertIn("Run provision without --only, or --only first first.", error)

    def test_only_runs_target_after_converged_check_only_closure(self) -> None:
        prerequisite = ScriptStep("first", [result(Disposition.CONVERGED)])
        target = ScriptStep(
            "second",
            [result(Disposition.DRIFT), result(Disposition.CONVERGED)],
            requires=("first",),
        )
        code, output, error = self.run_steps([prerequisite, target], args=arguments(only="second"))
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(prerequisite.apply_calls, 0)
        self.assertEqual(target.apply_calls, 1)
        self.assertIn("second: applied", output)

    def test_missing_site_blocks_site_steps_but_does_not_fail(self) -> None:
        step = ScriptStep("site-step", [result(Disposition.CONVERGED)], needs_site=True)
        code, output, error = self.run_steps([step])
        self.assertEqual(code, 0)
        self.assertEqual(step.check_calls, 0)
        self.assertIn("site-step: blocked", output)
        self.assertIn("Write /etc/gideon/site.yaml", output)
        self.assertEqual(error, "")

    def test_invalid_site_refuses_before_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site_path = str(Path(directory) / "site.yaml")
            host = FakeHost(files={site_path: "unknown: true\n"})
            step = ScriptStep("step", [result(Disposition.CONVERGED)])
            code, output, error = self.run_steps([step], host=host, site_path=site_path)
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertEqual(step.check_calls, 0)
        self.assertIn("Unknown key", error)
        self.assertIn("Fix:", error)

    def test_root_is_required_but_list_is_unprivileged(self) -> None:
        step = ScriptStep("step", [result(Disposition.CONVERGED)])
        code, output, error = self.run_steps(
            [step], args=arguments(listing=True), host=FakeHost(euid=1000)
        )
        self.assertEqual(code, 0)
        self.assertIn("step: summary for step", output)
        self.assertEqual(error, "")

        code, _, error = self.run_steps([step], host=FakeHost(euid=1000))
        self.assertEqual(code, 1)
        self.assertIn("root is required", error)

    def test_a_failed_child_command_explains_itself_in_the_row(self) -> None:
        """An apply that raises CalledProcessError shows the child's text, not a class name."""

        class AptStep(ScriptStep):
            def apply(self, context: ProvisionContext) -> str | None:
                del context
                raise subprocess.CalledProcessError(
                    100, ["apt-get", "install", "-y", "age"], "", "E: Unable to fetch some archives\n"
                )

        step = AptStep("host-tools", [result(Disposition.DRIFT)])
        code, output, _ = self.run_steps([step])
        self.assertEqual(code, 1)
        self.assertIn("host-tools: failed — apply failed: apt-get install -y age exited 100: E: Unable to fetch some archives", output)
        self.assertNotIn("CalledProcessError", output)

    def test_no_gpu_declares_before_skipping_all_gpu_only_steps(self) -> None:
        names = ("nvidia-driver", "nvidia-toolkit")
        steps = [
            ScriptStep(name, [result(Disposition.DRIFT)], gpu_host_only=True)
            for name in names
        ]
        host = FakeHost()
        args = arguments()
        args.no_gpu = True
        code, output, error = self.run_steps(steps, args=args, host=host)
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("/etc/gideon/no-gpu", host.files)
        self.assertIn("no-gpu: declared — /etc/gideon/no-gpu", output)
        for step in steps:
            self.assertIn(f"{step.name}: skipped — no-GPU host", output)
            self.assertEqual(step.check_calls, 0)
        self.assertIn(f"{len(names)} skipped", output)

        # The flag on a declared host is idempotent: nothing rewritten, said so.
        code, output, error = self.run_steps(steps, args=args, host=host)
        self.assertEqual(code, 0)
        self.assertIn("no-gpu: ok — already declared", output)
        self.assertEqual(len([c for c in host.calls if c[0] == "write_text"]), 1)

    def test_build_box_declares_before_steps_and_is_idempotent(self) -> None:
        step = ScriptStep("ordinary", [result(Disposition.CONVERGED)])
        host = FakeHost()
        args = arguments(build_box=True)
        code, output, error = self.run_steps([step], args=args, host=host)
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn(os.fspath(BUILD_BOX_PATH), host.files)
        self.assertLess(output.index("build-box: declared"), output.index("ordinary: ok"))

        code, output, error = self.run_steps([step], args=args, host=host)
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("build-box: ok — already declared", output)
        self.assertEqual(len([c for c in host.calls if c[0] == "write_text"]), 1)

    def test_build_box_dry_run_says_would_declare_without_writing(self) -> None:
        step = ScriptStep("ordinary", [result(Disposition.CONVERGED)])
        host = FakeHost()
        code, output, error = self.run_steps(
            [step], args=arguments(build_box=True, dry_run=True), host=host
        )
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("build-box: would declare", output)
        self.assertNotIn(os.fspath(BUILD_BOX_PATH), host.files)
        self.assertFalse(any(call[0] == "write_text" for call in host.calls))

    def test_bare_run_skips_build_box_only_but_runs_gpu_only_step(self) -> None:
        build_box_step = ScriptStep(
            "kvm", [result(Disposition.UNFIXABLE)], build_box_only=True
        )
        gpu_step = ScriptStep(
            "nvidia-driver", [result(Disposition.CONVERGED)], gpu_host_only=True
        )
        code, output, error = self.run_steps([build_box_step, gpu_step])
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn(f"kvm: skipped — {NOT_BUILD_BOX_DETAIL}", output)
        self.assertIn("nvidia-driver: ok", output)
        self.assertEqual(build_box_step.check_calls, 0)
        self.assertEqual(gpu_step.check_calls, 1)

    def test_no_gpu_host_skips_both_restricted_step_kinds(self) -> None:
        build_box_step = ScriptStep(
            "kvm", [result(Disposition.UNFIXABLE)], build_box_only=True
        )
        gpu_step = ScriptStep(
            "nvidia-driver", [result(Disposition.UNFIXABLE)], gpu_host_only=True
        )
        host = FakeHost(files={os.fspath(NO_GPU_PATH): ""})
        code, output, error = self.run_steps(
            [build_box_step, gpu_step], host=host
        )
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn(f"kvm: skipped — {NOT_BUILD_BOX_DETAIL}", output)
        self.assertIn("nvidia-driver: skipped — no-GPU host", output)
        self.assertEqual(build_box_step.check_calls, 0)
        self.assertEqual(gpu_step.check_calls, 0)

    def test_only_build_box_step_refuses_without_marker_and_runs_with_marker(self) -> None:
        step = ScriptStep(
            "kvm",
            [result(Disposition.DRIFT), result(Disposition.CONVERGED)],
            build_box_only=True,
        )
        code, output, error = self.run_steps(
            [step], args=arguments(only="kvm")
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn(BUILD_BOX_ONLY_FIX, error)
        self.assertEqual(step.check_calls, 0)

        host = FakeHost(files={os.fspath(BUILD_BOX_PATH): ""})
        code, output, error = self.run_steps(
            [step], args=arguments(only="kvm"), host=host
        )
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("kvm: applied", output)

    def test_build_box_only_prerequisite_skip_does_not_block_dependant(self) -> None:
        prerequisite = ScriptStep(
            "kvm", [result(Disposition.UNFIXABLE)], build_box_only=True
        )
        dependant = ScriptStep(
            "ordinary", [result(Disposition.CONVERGED)], requires=("kvm",)
        )
        code, output, error = self.run_steps([prerequisite, dependant])
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("ordinary: ok", output)
        self.assertEqual(dependant.check_calls, 1)

    def test_a_marker_pair_skips_every_restricted_step_and_refuses_both_flags(self) -> None:
        pair = {os.fspath(NO_GPU_PATH): "", os.fspath(BUILD_BOX_PATH): ""}
        build_box_step = ScriptStep(
            "kvm", [result(Disposition.UNFIXABLE)], build_box_only=True
        )
        gpu_step = ScriptStep(
            "nvidia-driver", [result(Disposition.UNFIXABLE)], gpu_host_only=True
        )
        code, output, error = self.run_steps(
            [build_box_step, gpu_step], host=FakeHost(files=pair)
        )
        self.assertEqual((code, error), (0, ""))
        self.assertIn(f"kvm: skipped — {NOT_BUILD_BOX_DETAIL}", output)
        self.assertIn("nvidia-driver: skipped — no-GPU host", output)
        self.assertEqual(build_box_step.check_calls + gpu_step.check_calls, 0)

        no_gpu_args = arguments()
        no_gpu_args.no_gpu = True
        for args, marker in (
            (arguments(build_box=True), NO_GPU_PATH),
            (no_gpu_args, BUILD_BOX_PATH),
        ):
            with self.subTest(marker=os.fspath(marker)):
                code, output, error = self.run_steps(
                    [build_box_step], args=args, host=FakeHost(files=pair)
                )
                self.assertEqual((code, output), (1, ""))
                self.assertNotIn("already declared", error)
                self.assertIn(f"Remove {marker}", error)

    def test_build_box_refuses_before_steps_on_a_no_gpu_host(self) -> None:
        step = ScriptStep("ordinary", [result(Disposition.CONVERGED)])
        host = FakeHost(files={os.fspath(NO_GPU_PATH): ""})
        code, output, error = self.run_steps(
            [step], args=arguments(build_box=True), host=host
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn(NO_GPU_DECLARED_PROBLEM, error)
        self.assertIn(os.fspath(NO_GPU_PATH), error)
        self.assertEqual(step.check_calls, 0)

    def test_no_gpu_only_refuses_a_gpu_only_step_and_names_the_marker(self) -> None:
        step = ScriptStep(
            "nvidia-driver", [result(Disposition.CONVERGED)], gpu_host_only=True
        )
        host = FakeHost(files={os.fspath(NO_GPU_PATH): ""})
        code, output, error = self.run_steps(
            [step], args=arguments(only="nvidia-driver"), host=host
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn(os.fspath(NO_GPU_PATH), error)
        self.assertEqual(step.check_calls, 0)

    def test_no_gpu_dry_run_reports_declaration_without_writing(self) -> None:
        gpu_step = ScriptStep(
            "nvidia-driver", [result(Disposition.UNFIXABLE)], gpu_host_only=True
        )
        step = ScriptStep("ordinary", [result(Disposition.CONVERGED)])
        host = FakeHost()
        args = arguments(dry_run=True)
        args.no_gpu = True
        code, output, error = self.run_steps([gpu_step, step], args=args, host=host)
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("no-gpu: would declare", output)
        self.assertIn("nvidia-driver: skipped — no-GPU host", output)
        self.assertEqual(gpu_step.check_calls, 0)
        self.assertNotIn("/etc/gideon/no-gpu", host.files)
        self.assertFalse(any(call[0] == "write_text" for call in host.calls))

    def test_bare_run_with_marker_skips_gpu_only_steps(self) -> None:
        step = ScriptStep(
            "nvidia-driver", [result(Disposition.UNFIXABLE)], gpu_host_only=True
        )
        host = FakeHost(files={os.fspath(NO_GPU_PATH): ""})
        code, output, error = self.run_steps([step], host=host)
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("nvidia-driver: skipped — no-GPU host", output)
        self.assertEqual(step.check_calls, 0)

    def test_no_gpu_refuses_when_a_pci_vendor_is_nvidia(self) -> None:
        vendor = "/sys/bus/pci/devices/0000:01:00.0/vendor"
        host = FakeHost(files={vendor: "0x10de\n"})
        step = ScriptStep("step", [result(Disposition.CONVERGED)])
        args = arguments()
        args.no_gpu = True
        code, output, error = self.run_steps([step], args=args, host=host)
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("gideon host provision: this host has an NVIDIA device", error)
        self.assertIn("Fix: Install the driver with host provision --only nvidia-driver", error)
        self.assertNotIn("/etc/gideon/no-gpu", host.files)
        self.assertEqual(step.check_calls, 0)

        # --dry-run refuses the same way: a GPU host is never "would declare".
        args = arguments(dry_run=True)
        args.no_gpu = True
        code, output, error = self.run_steps([step], args=args, host=host)
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("this host has an NVIDIA device", error)

    def test_only_prerequisite_skipped_for_no_gpu_is_satisfied(self) -> None:
        prerequisite = ScriptStep(
            "gpu-prerequisite", [result(Disposition.UNFIXABLE)], gpu_host_only=True
        )
        target = ScriptStep(
            "ordinary", [result(Disposition.CONVERGED)], requires=("gpu-prerequisite",)
        )
        host = FakeHost(files={"/etc/gideon/no-gpu": ""})
        code, output, error = self.run_steps(
            [prerequisite, target], args=arguments(only="ordinary"), host=host
        )
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("ordinary: ok", output)
        self.assertEqual(target.check_calls, 1)


if __name__ == "__main__":
    unittest.main()
