"""Contracts for the two-phase preflight runner."""

import contextlib
import io
import os
import subprocess
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path

from gideon.host.checks import (
    CHECKS,
    CheckReport,
    PreflightCheck,
    PreflightContext,
    Severity,
)
from gideon.host.checks.artifacts import JurisdictionCheck
from gideon.host.corpus.lockfile import load_lockfile
from gideon.host.courts import CourtMap
from gideon.host.nogpu import BUILD_BOX_PATH, NO_GPU_PATH, NOT_BUILD_BOX_DETAIL
from gideon.host.preflight import ObservedRow, run_preflight
from gideon.host.site import load_site
from gideon.host.steps import CheckResult, Disposition, ProvisionContext, Step
from gideon.host.sysio import Command, PathLike

SITE_PATH = "/etc/gideon/site.yaml"
ROOT = Path(__file__).resolve().parents[1]
HOST_LOCK_PATH = ROOT / "host.lock"
EGRESS_PATH = ROOT / "config/egress.yaml"
MODELS_LOCK_PATH = ROOT / "models.lock"
COURTS_PATH = ROOT / "courts.yaml"
LOCKFILES_PATH = ROOT / "tests/fixtures/preflight/lockfiles"
BROKEN_LOCKFILES_PATH = ROOT / "tests/fixtures/preflight/broken-lockfiles"
EXAMPLE_SITE = (ROOT / "config/site.example.yaml").read_text()
HOST_LOCK_TEXT = HOST_LOCK_PATH.read_text()
EGRESS_TEXT = EGRESS_PATH.read_text()
MODELS_LOCK_TEXT = MODELS_LOCK_PATH.read_text()
COURTS_TEXT = COURTS_PATH.read_text()


def fixture_files(directory: Path) -> dict[str, str]:
    """Serve a lockfile directory and its companions through FakeHost."""

    files = {str(directory): ""}
    files.update(
        (str(path), path.read_text())
        for path in directory.rglob("*")
        if path.is_file()
    )
    return files


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

    def read_bytes(self, path: PathLike) -> bytes:
        key = os.fspath(path)
        if key in self.read_errors:
            raise self.read_errors[key]
        if key in self.files:
            return self.files[key].encode("utf-8")
        return Path(key).read_bytes()

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
    lockfiles_path: PathLike | None = None,
    advisory_fix: str | None = None,
    observer: Callable[[ObservedRow], None] | None = None,
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
            lockfiles_path=lockfiles_path,
            advisory_fix=advisory_fix,
            observer=observer,
        )
    return code, stdout.getvalue(), stderr.getvalue()


class LockfileReading(unittest.TestCase):
    """The committed court map and a fictitious cut meet at the pre-run boundary."""

    def test_valid_cut_reaches_jurisdiction_and_warns_on_unshipped_home_courts(self) -> None:
        paths = list(LOCKFILES_PATH.glob("*.yaml"))
        self.assertEqual(len(paths), 1)
        loaded = load_lockfile(paths[0])
        self.assertTrue(loaded.ok, loaded.errors)
        assert loaded.lockfile is not None
        site_result = load_site(Path(SITE_PATH), host=FakeHost(files={SITE_PATH: EXAMPLE_SITE}))
        assert site_result.config is not None
        home = site_result.config.jurisdiction

        code, out, error = preflight(
            files=fixture_files(LOCKFILES_PATH),
            lockfiles_path=LOCKFILES_PATH,
            checks=[JurisdictionCheck()],
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(error, "")
        self.assertIn(f"jurisdiction: warn — lockfile {loaded.lockfile.label}", out)
        for identifier in (*home.districts, *home.states):
            self.assertIn(repr(identifier), out)
        self.assertIn("district tier is not in the lockfile", out)
        self.assertIn("no appellate court of the state", out)

    def test_sidecar_off_its_pin_refuses_before_rows(self) -> None:
        code, out, error = preflight(
            files=fixture_files(BROKEN_LOCKFILES_PATH),
            lockfiles_path=BROKEN_LOCKFILES_PATH,
            checks=[JurisdictionCheck()],
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("sidecar digest differs from its pin", error)
        self.assertIn("Fix: Restore the lockfile and its companion files", error)

    def test_unknown_lockfile_court_refuses_before_rows(self) -> None:
        paths = list(LOCKFILES_PATH.glob("*.yaml"))
        self.assertEqual(len(paths), 1)
        loaded = load_lockfile(paths[0])
        assert loaded.lockfile is not None
        files = fixture_files(LOCKFILES_PATH)
        original = f"    - {loaded.lockfile.courts[0]}\n"
        self.assertIn(original, files[str(paths[0])])
        files[str(paths[0])] = files[str(paths[0])].replace(
            original, "    - fictional-court\n"
        )

        code, out, error = preflight(
            files=files,
            lockfiles_path=LOCKFILES_PATH,
            checks=[JurisdictionCheck()],
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("unknown court id 'fictional-court'", error)
        self.assertIn("Fix: Restore the lockfile and its companion files", error)


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
        self.assertTrue(error.rstrip().endswith("docs/runbooks/release-files.md §4."))

    def test_malformed_models_lock_refuses_before_check_rows(self) -> None:
        path = "/tmp/gideon-test-models.lock"
        code, out, error = preflight(
            models_path=path,
            files={path: "unknown: true\n"},
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Unknown key 'unknown'", error)
        self.assertTrue(error.rstrip().endswith("docs/runbooks/release-files.md §4."))

    def test_missing_court_map_refuses_before_check_rows(self) -> None:
        path = "/nonexistent-gideon-test/courts.yaml"
        code, out, error = preflight(courts_path=path)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("court map is missing", error)
        self.assertTrue(error.rstrip().endswith("docs/runbooks/release-files.md §6."))

    def test_unreadable_court_map_refuses_before_check_rows(self) -> None:
        path = os.fspath(COURTS_PATH)
        code, out, error = preflight(
            read_errors={path: PermissionError("test permission")}
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("court map is unreadable due to permissions", error)
        self.assertTrue(error.rstrip().endswith("docs/runbooks/release-files.md §6."))

    def test_malformed_court_map_refuses_before_check_rows(self) -> None:
        path = "/tmp/gideon-test-courts.yaml"
        code, out, error = preflight(
            courts_path=path,
            files={path: "unknown: true\n"},
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Unknown key 'unknown'", error)
        self.assertTrue(error.rstrip().endswith("docs/runbooks/release-files.md §6."))

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
    """Provision's check pass enforces total convergence."""

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


class AdvisoryReading(unittest.TestCase):
    """The advisory reading reports unconverged steps as warnings, never refusals."""

    def test_every_unconverged_disposition_warns_with_supplied_fix(self) -> None:
        fix = "The new release will converge this step."
        for disposition in (
            Disposition.DRIFT,
            Disposition.UNFIXABLE,
            Disposition.PENDING_INPUT,
            Disposition.REBOOT_REQUIRED,
        ):
            with self.subTest(disposition=disposition):
                step = FixedStep("step", disposition)
                code, out, error = preflight(steps=[step], advisory_fix=fix)
                self.assertEqual(code, 0, error)
                self.assertEqual(
                    out,
                    f"step: warn — {disposition.value} Fix: {fix}\n"
                    "Summary: 1 check(s); 1 warn.\n",
                )
                self.assertEqual(step.apply_calls, 0)

    def test_raising_step_warns_with_its_detail_and_supplied_fix(self) -> None:
        class RaisingStep(FixedStep):
            def check(self, context: ProvisionContext) -> CheckResult:
                del context
                raise RuntimeError("broken check")

        code, out, error = preflight(
            steps=[RaisingStep("raised", Disposition.DRIFT)],
            advisory_fix="Wait for the new release.",
        )
        self.assertEqual(code, 0, error)
        self.assertIn("raised: warn — check raised RuntimeError: broken check", out)
        self.assertIn("Fix: Wait for the new release.", out)
        self.assertNotIn("re-run provision", out)

    def test_install_time_refusal_still_blocks_and_summary_counts_warn(self) -> None:
        code, out, error = preflight(
            steps=[FixedStep("drifted", Disposition.DRIFT)],
            checks=[FixedCheck("broken", CheckReport(Severity.REFUSE, "bad", "repair"))],
            advisory_fix="New release handles drift.",
        )
        self.assertEqual(code, 1, error)
        self.assertIn("drifted: warn — drift Fix: New release handles drift.", out)
        self.assertIn("broken: refuse — bad Fix: repair", out)
        self.assertIn("Summary: 2 check(s); 1 warn, 1 refuse.", out)

    def test_skipped_rows_remain_passes(self) -> None:
        gpu_step = FixedStep("gpu", Disposition.DRIFT, gpu_host_only=True)
        build_step = FixedStep("build", Disposition.DRIFT, build_box_only=True)
        code, out, error = preflight(
            steps=[gpu_step, build_step],
            files={os.fspath(NO_GPU_PATH): "declared\n"},
            advisory_fix="New release handles drift.",
        )
        self.assertEqual(code, 0, error)
        self.assertIn("gpu: pass — skipped: no-GPU host", out)
        self.assertIn(f"build: pass — skipped: {NOT_BUILD_BOX_DETAIL}", out)
        self.assertIn("Summary: 2 check(s); 2 pass.", out)
        self.assertEqual((gpu_step.check_calls, build_step.check_calls), (0, 0))


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

    def test_observer_receives_each_printed_row_in_order_and_phase(self) -> None:
        rows: list[ObservedRow] = []
        code, out, error = preflight(
            steps=[
                FixedStep("good", Disposition.CONVERGED),
                FixedStep("drifted", Disposition.DRIFT),
            ],
            checks=[FixedCheck("warning", CheckReport(Severity.WARN, "caution"))],
            advisory_fix="New release handles drift.",
            observer=rows.append,
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(
            [(row.name, row.report.severity, row.provision_step) for row in rows],
            [
                ("good", Severity.PASS, True),
                ("drifted", Severity.WARN, True),
                ("warning", Severity.WARN, False),
            ],
        )
        self.assertEqual(rows[1].report.detail, "drift")
        self.assertEqual(rows[1].report.fix, "New release handles drift.")
        self.assertLess(out.index("good:"), out.index("drifted:"))
        self.assertLess(out.index("drifted:"), out.index("warning:"))

    def test_observer_sees_default_refusals_and_no_pre_run_rows(self) -> None:
        rows: list[ObservedRow] = []
        code, _, _ = preflight(
            steps=[FixedStep("drifted", Disposition.DRIFT)], observer=rows.append
        )
        self.assertEqual(code, 1)
        self.assertEqual(rows[0].report.severity, Severity.REFUSE)

        rows.clear()
        code, out, error = preflight(host=FakeHost(euid=1000), observer=rows.append)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("root is required", error)
        self.assertEqual(rows, [])
