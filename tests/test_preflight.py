"""Contracts for the two-phase preflight runner (spec §1.5)."""

import contextlib
import io
import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.host.checks import (
    CHECKS,
    CheckReport,
    PreflightCheck,
    PreflightContext,
    Severity,
)
from gideon.host.courts import CourtMap
from gideon.host.nogpu import BUILD_BOX_PATH, NO_GPU_PATH, NOT_BUILD_BOX_DETAIL
from gideon.host.preflight import run_preflight
from gideon.host.steps import CheckResult, Disposition, ProvisionContext, Step
from gideon.host.sysio import Command, PathLike

SITE_PATH = "/etc/gideon/site.yaml"
ROOT = Path(__file__).resolve().parents[1]
HOST_LOCK_PATH = ROOT / "host.lock"
EGRESS_PATH = ROOT / "config/egress.yaml"
MODELS_LOCK_PATH = ROOT / "models.lock"
COURTS_PATH = ROOT / "courts.yaml"
EXAMPLE_SITE = (ROOT / "config/site.example.yaml").read_text()
HOST_LOCK_TEXT = HOST_LOCK_PATH.read_text()
EGRESS_TEXT = EGRESS_PATH.read_text()
MODELS_LOCK_TEXT = MODELS_LOCK_PATH.read_text()
COURTS_TEXT = COURTS_PATH.read_text()


class FakeHost:
    """A root-looking Host that reads only explicitly supplied fixture files."""

    def __init__(
        self,
        *,
        files: dict[str, str] | None = None,
        commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
        read_errors: Mapping[str, OSError] | None = None,
        euid: int = 0,
    ) -> None:
        self.files = files or {}
        self.commands = dict(commands or {})
        self.read_errors = dict(read_errors or {})
        self.euid = euid
        self.calls: list[tuple[str, ...]] = []

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
        del check, input, cwd, env, timeout
        self.calls.append(tuple(argv))
        return self.commands.get(tuple(argv), subprocess.CompletedProcess(list(argv), 0, "", ""))

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key in self.read_errors:
            raise self.read_errors[key]
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
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        root = Path(path)
        return [Path(name).name for name in self.files if Path(name).parent == root]

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


class FixedStep(Step):
    def __init__(
        self,
        name: str,
        disposition: Disposition,
        *,
        needs_site: bool = False,
        gpu_host_only: bool = False,
        build_box_only: bool = False,
    ) -> None:
        self.name = name
        self.summary = f"summary for {name}"
        self.needs_site = needs_site
        self.gpu_host_only = gpu_host_only
        self.build_box_only = build_box_only
        self.disposition = disposition
        self.apply_calls = 0
        self.check_calls = 0

    def check(self, context: ProvisionContext) -> CheckResult:
        del context
        self.check_calls += 1
        return CheckResult(self.disposition, self.disposition.value, "step fix")

    def apply(self, context: ProvisionContext) -> None:
        del context
        self.apply_calls += 1


class FixedCheck(PreflightCheck):
    def __init__(self, name: str, report: CheckReport) -> None:
        self.name = name
        self.summary = f"summary for {name}"
        self.report = report

    def run(self, context: PreflightContext) -> CheckReport:
        del context
        return self.report


class RaisingCheck(PreflightCheck):
    name = "raising"
    summary = "raises"

    def run(self, context: PreflightContext) -> CheckReport:
        del context
        raise RuntimeError("boom")


def preflight(
    *,
    host: FakeHost | None = None,
    steps: list[Step] | None = None,
    checks: list[PreflightCheck] | None = None,
    site_path: str = SITE_PATH,
    models_path: PathLike | None = None,
    files: Mapping[str, str] | None = None,
    commands: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
    read_errors: Mapping[str, OSError] | None = None,
    courts_path: PathLike | None = None,
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    default_files = {
        SITE_PATH: EXAMPLE_SITE,
        str(HOST_LOCK_PATH): HOST_LOCK_TEXT,
        str(EGRESS_PATH): EGRESS_TEXT,
        str(MODELS_LOCK_PATH): MODELS_LOCK_TEXT,
        str(COURTS_PATH): COURTS_TEXT,
    }
    default_files.update(files or {})
    io_host = host or FakeHost(
        files=default_files, commands=commands, read_errors=read_errors
    )
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = run_preflight(
            None,
            host=io_host,
            steps=steps if steps is not None else [],
            checks=checks if checks is not None else [],
            site_path=site_path,
            models_path=models_path,
            courts_path=courts_path,
        )
    return code, stdout.getvalue(), stderr.getvalue()


class Refusals(unittest.TestCase):
    def test_non_root_refuses_with_fix(self) -> None:
        code, _, error = preflight(host=FakeHost(euid=1000))
        self.assertEqual(code, 1)
        self.assertIn("root is required", error)
        self.assertIn("Fix:", error)

    def test_missing_site_file_refuses_with_fix(self) -> None:
        # A path that exists nowhere: FakeHost.exists falls through to the
        # real filesystem, and a provisioned box has the real site file.
        code, _, error = preflight(
            host=FakeHost(), site_path="/nonexistent-gideon-test/site.yaml"
        )
        self.assertEqual(code, 1)
        self.assertIn("site file is missing", error)
        self.assertIn("Fix:", error)

    def test_invalid_site_file_refuses_with_fix(self) -> None:
        code, _, error = preflight(host=FakeHost(files={SITE_PATH: "unknown: true\n"}))
        self.assertEqual(code, 1)
        self.assertIn("Fix:", error)

    def test_missing_models_lock_refuses_before_check_rows(self) -> None:
        path = "/nonexistent-gideon-test/models.lock"
        code, out, error = preflight(models_path=path)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("models lock is missing", error)
        self.assertTrue(error.rstrip().endswith("docs/archi/host.md."))

    def test_malformed_models_lock_refuses_before_check_rows(self) -> None:
        path = "/tmp/gideon-test-models.lock"
        code, out, error = preflight(
            models_path=path,
            files={path: "unknown: true\n"},
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Unknown key 'unknown'", error)
        self.assertTrue(error.rstrip().endswith("docs/archi/host.md."))

    def test_missing_court_map_refuses_before_check_rows(self) -> None:
        path = "/nonexistent-gideon-test/courts.yaml"
        code, out, error = preflight(courts_path=path)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("court map is missing", error)
        self.assertTrue(error.rstrip().endswith("docs/archi/host.md."))

    def test_unreadable_court_map_refuses_before_check_rows(self) -> None:
        path = os.fspath(COURTS_PATH)
        code, out, error = preflight(
            read_errors={path: PermissionError("test permission")}
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("court map is unreadable due to permissions", error)
        self.assertTrue(error.rstrip().endswith("docs/archi/host.md."))

    def test_malformed_court_map_refuses_before_check_rows(self) -> None:
        path = "/tmp/gideon-test-courts.yaml"
        code, out, error = preflight(
            courts_path=path,
            files={path: "unknown: true\n"},
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Unknown key 'unknown'", error)
        self.assertTrue(error.rstrip().endswith("docs/archi/host.md."))

    def test_good_court_map_reaches_a_check(self) -> None:
        class ContextCheck(PreflightCheck):
            name = "context-courts"
            summary = "observe the court map"

            def __init__(self) -> None:
                self.courts: CourtMap | None = None

            def run(self, context: PreflightContext) -> CheckReport:
                self.courts = context.courts
                return CheckReport(Severity.PASS, "court map reached check")

        check = ContextCheck()
        code, out, error = preflight(checks=[check])
        self.assertEqual(code, 0, error)
        self.assertIn("court map reached check", out)
        self.assertIsNotNone(check.courts)


class PhaseA(unittest.TestCase):
    """Provision's check pass: total convergence is enforced (ticket 02 ruling 1)."""

    def test_converged_step_passes_and_unconverged_refuses(self) -> None:
        steps: list[Step] = [
            FixedStep("good", Disposition.CONVERGED),
            FixedStep("drifted", Disposition.DRIFT),
            FixedStep("stuck", Disposition.UNFIXABLE),
            FixedStep("rebooty", Disposition.REBOOT_REQUIRED),
        ]
        code, out, _ = preflight(steps=steps)
        self.assertEqual(code, 1)
        self.assertIn("good: pass", out)
        self.assertIn("drifted: refuse", out)
        self.assertIn("stuck: refuse", out)
        self.assertIn("rebooty: refuse", out)

    def test_refusing_step_line_carries_the_fix(self) -> None:
        _, out, _ = preflight(steps=[FixedStep("drifted", Disposition.DRIFT)])
        self.assertIn("Fix: step fix", out)

    def test_apply_is_never_called(self) -> None:
        step = FixedStep("drifted", Disposition.DRIFT)
        preflight(steps=[step])
        self.assertEqual(step.apply_calls, 0)

    def test_gpu_only_step_is_a_skipped_pass_on_a_no_gpu_host(self) -> None:
        step = FixedStep("gpu", Disposition.DRIFT, gpu_host_only=True)
        host = FakeHost(files={SITE_PATH: EXAMPLE_SITE, "/etc/gideon/no-gpu": ""})
        code, out, _ = preflight(host=host, steps=[step])
        self.assertEqual(code, 0)
        self.assertIn("gpu: pass — skipped: no-GPU host", out)
        self.assertEqual(step.check_calls, 0)

    def test_gpu_only_step_is_checked_on_a_gpu_host(self) -> None:
        step = FixedStep("gpu", Disposition.DRIFT, gpu_host_only=True)
        code, out, _ = preflight(steps=[step])
        self.assertEqual(code, 1)
        self.assertIn("gpu: refuse", out)
        self.assertEqual(step.check_calls, 1)

    def test_build_box_only_step_is_a_skipped_pass_without_marker(self) -> None:
        step = FixedStep("build", Disposition.DRIFT, build_box_only=True)
        code, out, _ = preflight(steps=[step])
        self.assertEqual(code, 0)
        self.assertIn(f"build: pass — skipped: {NOT_BUILD_BOX_DETAIL}", out)
        self.assertEqual(step.check_calls, 0)

    def test_build_box_only_step_is_skipped_under_a_marker_pair(self) -> None:
        step = FixedStep("build", Disposition.DRIFT, build_box_only=True)
        host = FakeHost(
            files={
                SITE_PATH: EXAMPLE_SITE,
                os.fspath(BUILD_BOX_PATH): "",
                os.fspath(NO_GPU_PATH): "",
            }
        )
        code, out, _ = preflight(host=host, steps=[step])
        self.assertIn(f"build: pass — skipped: {NOT_BUILD_BOX_DETAIL}", out)
        self.assertEqual(step.check_calls, 0)

    def test_build_box_only_step_is_checked_with_marker(self) -> None:
        step = FixedStep("build", Disposition.DRIFT, build_box_only=True)
        host = FakeHost(
            files={SITE_PATH: EXAMPLE_SITE, os.fspath(BUILD_BOX_PATH): ""}
        )
        code, out, _ = preflight(host=host, steps=[step])
        self.assertEqual(code, 1)
        self.assertIn("build: refuse", out)
        self.assertEqual(step.check_calls, 1)


class PhaseB(unittest.TestCase):
    def test_real_gpu_checks_skip_on_no_gpu_and_count_as_passes(self) -> None:
        data_volume = ("df", "-B1", "--output=size,avail", "/data")
        code, out, _ = preflight(
            files={"/etc/gideon/no-gpu": "declared\n"},
            commands={
                data_volume: subprocess.CompletedProcess(
                    list(data_volume), 0, " Size Avail\n1 1\n", ""
                )
            },
            checks=[
                check
                for check in CHECKS
                if check.name in {"hardware-profile", "data-volume"}
            ],
        )
        self.assertEqual(code, 0)
        self.assertIn("hardware-profile: pass — skipped: no-GPU host", out)
        self.assertIn("data-volume: pass — skipped: no-GPU host", out)
        self.assertIn("Summary: 2 check(s); 2 pass.", out)

    def test_warn_and_inert_never_block(self) -> None:
        checks: list[PreflightCheck] = [
            FixedCheck("warned", CheckReport(Severity.WARN, "meh", "warn fix")),
            FixedCheck("inactive", CheckReport(Severity.INERT, "ships later")),
            FixedCheck("fine", CheckReport(Severity.PASS, "ok")),
        ]
        code, out, _ = preflight(checks=checks)
        self.assertEqual(code, 0)
        self.assertIn("warned: warn", out)
        self.assertIn("inactive: inert", out)
        self.assertIn("fine: pass", out)

    def test_refusal_blocks_and_prints_fix(self) -> None:
        checks: list[PreflightCheck] = [
            FixedCheck("broken", CheckReport(Severity.REFUSE, "bad", "do the thing")),
        ]
        code, out, _ = preflight(checks=checks)
        self.assertEqual(code, 1)
        self.assertIn("broken: refuse", out)
        self.assertIn("Fix: do the thing", out)

    def test_raising_check_becomes_a_refusal(self) -> None:
        code, out, _ = preflight(checks=[RaisingCheck()])
        self.assertEqual(code, 1)
        self.assertIn("raising: refuse", out)
        self.assertIn("RuntimeError", out)
        self.assertIn("Fix:", out)


class Reporting(unittest.TestCase):
    def test_steps_report_before_checks_and_summary_counts(self) -> None:
        steps: list[Step] = [FixedStep("good", Disposition.CONVERGED)]
        checks: list[PreflightCheck] = [
            FixedCheck("warned", CheckReport(Severity.WARN, "meh", "warn fix")),
        ]
        code, out, _ = preflight(steps=steps, checks=checks)
        self.assertEqual(code, 0)
        self.assertLess(out.index("good: pass"), out.index("warned: warn"))
        self.assertIn("Summary: 2 check(s); 1 pass, 1 warn.", out)

    def test_all_reports_render_even_after_a_refusal(self) -> None:
        """Preflight never halts: the operator fixes everything in one pass."""

        steps: list[Step] = [
            FixedStep("drifted", Disposition.DRIFT),
            FixedStep("good", Disposition.CONVERGED),
        ]
        checks: list[PreflightCheck] = [
            FixedCheck("fine", CheckReport(Severity.PASS, "ok")),
        ]
        code, out, _ = preflight(steps=steps, checks=checks)
        self.assertEqual(code, 1)
        self.assertIn("good: pass", out)
        self.assertIn("fine: pass", out)
