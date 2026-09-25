"""The install command's contracts: phase order, refusals, rows, and the URL."""

import argparse
import ast
import contextlib
import io
import os
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from gideon.host import audit, backuplock, backupset, install, nogpu
from gideon.host.report import StageResult
from gideon.host.site import load_site
from gideon.host.sysio import LockingHost, PathLike

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
RENDERED = "/etc/gideon/rendered"
PHASES = ["preflight", "apply", "reconcile", "engine-verify", "backup", "drill", "audit", "url"]
NESTED = ["preflight", "apply", "reconcile", "engine-verify", "backup", "drill"]
SET_LABEL = "20260903T010000Z"
NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
_site = load_site(EXAMPLE).config
assert _site is not None
HOSTNAME = _site.hostname


def manifest_json(label: str) -> str:
    entry = backupset.Entry("config.yaml", "f", 7, 0, 0, 0o644, 100.0, "a" * 64)
    return backupset.Manifest(
        1,
        label,
        backupset.Kind.NIGHTLY,
        NOW - timedelta(minutes=5),
        NOW,
        "1000.0.0",
        "/work/GIDEON",
        "commit",
        "gideon.test",
        None,
        "backup-label",
        "full",
        NOW,
        {"gideon": {"public.users": 2}, "openwebui": {}},
        {"etc-gideon": (entry,)},
        "c" * 64,
        ("age1" + "a" * 58,),
        backupset.LinkVerdict(1, 1),
        "d" * 64,
    ).to_json()


class FakeHost:
    """Root by default, the example site file, and an optional staging listing."""

    def __init__(
        self,
        *,
        euid: int = 0,
        site: bool = True,
        sets: Mapping[str, str] | None = None,
        lock_holder: str | None = None,
        lock_error: OSError | None = None,
    ) -> None:
        self.euid = euid
        self.files: dict[str, str] = {}
        if site:
            self.files[str(EXAMPLE)] = EXAMPLE.read_text()
        self.sets = dict(sets or {})
        self.lock_holder = lock_holder
        self.lock_error = lock_error
        self.locks: dict[str, str] = {}
        self.lock_records: list[str] = []
        self.lock_log: list[tuple[str, str]] = []
        for label, text in self.sets.items():
            self.files[os.path.join(backupset.set_dir(label), backupset.MANIFEST_NAME)] = text

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        try:
            return self.files[os.fspath(path)]
        except KeyError as exc:
            raise FileNotFoundError(os.fspath(path)) from exc

    def listdir(self, path: PathLike) -> list[str]:
        if os.fspath(path) == backupset.SETS_DIR and self.sets:
            return list(self.sets)
        raise FileNotFoundError(os.fspath(path))

    def geteuid(self) -> int:
        return self.euid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        self.lock_log.append(("take", key))
        self.lock_records.append(record)
        if self.lock_error is not None:
            raise self.lock_error
        if self.lock_holder is not None:
            return self.lock_holder
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = record
        return None

    def release_lock(self, path: PathLike) -> None:
        key = os.fspath(path)
        self.lock_log.append(("release", key))
        self.locks.pop(key, None)


class FakeAudit:
    """Records rows; the write at index ``fail_from`` and after report a problem."""

    def __init__(self, *, fail_from: int | None = None) -> None:
        self.fail_from = fail_from
        self.rows: list[audit.AuditRow] = []
        self.writes = 0

    def write_rows(
        self, io: FakeHost, rendered_dir: PathLike, rows: tuple[audit.AuditRow, ...]
    ) -> str | None:
        del io, rendered_dir
        self.writes += 1
        if self.fail_from is not None and self.writes > self.fail_from:
            return "audit writer failed: exit 1"
        self.rows.extend(rows)
        return None


class FakeRunners:
    """Every nested command succeeds except ``failing``, which exits ``code``."""

    def __init__(
        self,
        *,
        failing: str | None = None,
        code: int = 3,
        engine_rows: tuple[StageResult, ...] = (),
    ) -> None:
        self.failing = failing
        self.code = code
        self.engine_rows = engine_rows
        self.calls: list[tuple[str, argparse.Namespace]] = []

    def mapping(self) -> dict[str, install.Runner]:
        return {name: self._runner(name) for name in NESTED}

    def _runner(self, name: str) -> install.Runner:
        def run(child: argparse.Namespace) -> int:
            self.calls.append((name, child))
            if name == "engine-verify":
                observe = getattr(child, "observe", None)
                if observe is not None:
                    for row in self.engine_rows:
                        observe(row)
            return self.code if name == self.failing else 0

        return run


def row_names(text: str) -> list[str]:
    return [
        line.split(":", 1)[0]
        for line in text.splitlines()
        if line.split(":", 1)[0] in PHASES and " — " in line
    ]


class InstallTests(unittest.TestCase):
    def run_install(
        self,
        host: Any,
        runners: FakeRunners | None = None,
        audit_backend: FakeAudit | None = None,
    ) -> tuple[int, str, str, FakeRunners, FakeAudit]:
        nested = runners or FakeRunners()
        backend = audit_backend or FakeAudit()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = install.run_install(
                argparse.Namespace(command_path="install"),
                host=host,
                site_path=EXAMPLE,
                rendered_dir=RENDERED,
                runners=nested.mapping(),
                audit=backend,
            )
        return code, out.getvalue(), err.getvalue(), nested, backend

    def test_phases_run_in_order_and_the_url_is_the_last_line(self) -> None:
        host = FakeHost(sets={SET_LABEL: manifest_json(SET_LABEL)})
        code, out, err, runners, backend = self.run_install(host)

        self.assertEqual(code, 0, out + err)
        self.assertEqual(row_names(out), PHASES)
        self.assertEqual([name for name, _ in runners.calls], NESTED)
        children = dict(runners.calls)
        self.assertEqual(children["reconcile"].command_path, "users reconcile")
        self.assertTrue(children["reconcile"].now)
        self.assertEqual(children["backup"].command_path, "backup run")
        self.assertFalse(children["backup"].full)
        self.assertIsNone(children["backup"].label)
        self.assertIn("engine-verify: ok — engine verify completed", out)
        self.assertIn("Summary: 8 phase(s); 8 ok.", out)
        self.assertEqual(out.splitlines()[-1], f"https://{HOSTNAME}/")

        self.assertEqual([row.kind for row in backend.rows], ["install", "install"])
        intent, applied = backend.rows
        self.assertEqual(intent.detail["phase"], "intent")
        self.assertEqual(intent.detail["hostname"], HOSTNAME)
        self.assertEqual(applied.detail["phase"], "applied")
        self.assertEqual(applied.detail["set_label"], SET_LABEL)
        durations = applied.detail["durations"]
        assert isinstance(durations, Mapping)
        self.assertEqual(list(durations), PHASES[:6])
        self.assertEqual(intent.run_id, applied.run_id)
        for row in backend.rows:
            self.assertIsNone(row.actor_user_id)
            self.assertEqual(row.kb_ids, ())

    def test_in_process_holders_take_and_release_in_turn(self) -> None:
        host = FakeHost()
        output = io.StringIO()
        instant = NOW

        def apply_standin(
            _args: object, *, host: FakeHost, **_kwargs: object
        ) -> int:
            locking_host = cast(LockingHost, host)
            claim = backuplock.claim(
                locking_host, command="gideon apply", now=instant
            )
            self.assertIsNone(claim.refusal)
            backuplock.release_claim(locking_host, claim)
            return 0

        def engine_standin(
            _args: object, *, host: FakeHost, **_kwargs: object
        ) -> int:
            locking_host = cast(LockingHost, host)
            claim = backuplock.claim(
                locking_host, command="gideon engine verify", now=instant
            )
            self.assertIsNone(claim.refusal)
            backuplock.release_claim(locking_host, claim)
            return 0

        def reconcile_standin(
            _args: object, *, host: FakeHost, **_kwargs: object
        ) -> int:
            self.assertNotIn(backuplock.ENGINE_LOCK.path, host.locks)
            return 0

        def success(*_args: object, **_kwargs: object) -> int:
            return 0

        backend = FakeAudit()
        with (
            patch.object(install.apply, "run_apply", apply_standin),
            patch.object(install.engine, "run_engine_verify", engine_standin),
            patch.object(install.preflight, "run_preflight", success),
            patch.object(install.users, "run_reconcile", reconcile_standin),
            patch.object(install.backup, "run_backup_run", success),
            patch.object(install.drill, "run_backup_drill", success),
            contextlib.redirect_stdout(output),
        ):
            code = install.run_install(
                argparse.Namespace(command_path="install"),
                host=cast(LockingHost, host),
                site_path=EXAMPLE,
                rendered_dir=RENDERED,
                audit=backend,
            )

        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(
            host.lock_log,
            [
                ("take", backuplock.ENGINE_LOCK.path),
                ("release", backuplock.ENGINE_LOCK.path),
                ("take", backuplock.ENGINE_LOCK.path),
                ("release", backuplock.ENGINE_LOCK.path),
            ],
        )
        records = [backuplock.parse(value) for value in host.lock_records]
        self.assertEqual(
            [record.command for record in records if record is not None],
            ["gideon apply", "gideon engine verify"],
        )

    def test_refused_apply_standin_stops_install_without_releasing_foreign_lock(self) -> None:
        holder = backuplock.Record(
            "eval run fixture", os.getpid() + 1, NOW
        )
        host = FakeHost(lock_holder=holder.to_json())
        output = io.StringIO()

        def refused_apply(
            _args: object, *, host: FakeHost, **_kwargs: object
        ) -> int:
            locking_host = cast(LockingHost, host)
            claim = backuplock.claim(
                locking_host, command="gideon apply", now=NOW
            )
            self.assertIsNotNone(claim.refusal)
            return 1

        backend = FakeAudit()
        with (
            patch.object(install.preflight, "run_preflight", lambda *_args, **_kwargs: 0),
            patch.object(install.apply, "run_apply", refused_apply),
            contextlib.redirect_stdout(output),
        ):
            code = install.run_install(
                argparse.Namespace(command_path="install"),
                host=cast(LockingHost, host),
                site_path=EXAMPLE,
                rendered_dir=RENDERED,
                audit=backend,
            )

        self.assertEqual(code, 1)
        self.assertIn("apply: refuse — apply refused (exit 1)", output.getvalue())
        self.assertEqual(
            host.lock_log, [("take", backuplock.ENGINE_LOCK.path)]
        )
        self.assertEqual(backend.rows, [])

    def test_no_staging_listing_leaves_the_set_label_empty(self) -> None:
        code, _, _, _, backend = self.run_install(FakeHost())
        self.assertEqual(code, 0)
        self.assertIsNone(backend.rows[-1].detail["set_label"])

    def test_no_gpu_marker_does_not_change_nested_runner_namespaces(self) -> None:
        host = FakeHost()
        host.files[os.fspath(nogpu.NO_GPU_PATH)] = "declared\n"

        code, _, _, runners, _ = self.run_install(host)

        self.assertEqual(code, 0)
        children = dict(runners.calls)
        self.assertEqual(children["reconcile"].command_path, "users reconcile")
        self.assertTrue(children["reconcile"].now)
        self.assertEqual(children["backup"].command_path, "backup run")
        self.assertFalse(children["backup"].full)
        self.assertIsNone(children["backup"].label)
        for child in children.values():
            self.assertNotIn("no_gpu", vars(child))

    def test_gate_row_uses_the_no_gpu_skip_observation(self) -> None:
        skipped = StageResult("engine", True, "skipped — fixture host", "")
        code, out, _, runners, _ = self.run_install(
            FakeHost(), FakeRunners(engine_rows=(skipped,))
        )

        self.assertEqual(code, 0, out)
        self.assertIn(
            f"engine-verify: ok — {install.ENGINE_VERIFY_SKIPPED_DETAIL}", out
        )
        self.assertEqual([name for name, _ in runners.calls], NESTED)

    def test_gate_row_counts_observed_checks(self) -> None:
        rows = (
            StageResult("preconditions", True, "ready", ""),
            StageResult("needle", True, "checked", ""),
            StageResult("smoke", True, "checked", ""),
            StageResult("audit", True, "recorded", ""),
        )
        code, out, _, _, _ = self.run_install(FakeHost(), FakeRunners(engine_rows=rows))

        self.assertEqual(code, 0, out)
        self.assertIn("engine-verify: ok — engine verify completed (2 checks ok)", out)

    def test_gate_refusal_stops_before_backup_and_records_the_phase(self) -> None:
        rows = (
            StageResult("preconditions", True, "ready", ""),
            StageResult("smoke", False, "smoke failed", "fixture fix"),
            StageResult("audit", True, "recorded", ""),
        )
        code, out, _, runners, backend = self.run_install(
            FakeHost(), FakeRunners(failing="engine-verify", code=7, engine_rows=rows)
        )

        self.assertEqual(code, 1)
        self.assertIn(
            "engine-verify: refuse — engine verify refused (exit 7): smoke Fix: "
            "Do not go live. Run sudo python3 -m gideon engine verify, correct its refusal, "
            "then re-run install.",
            out,
        )
        self.assertEqual([name for name, _ in runners.calls], NESTED[:4])
        self.assertEqual(backend.rows[-1].detail["failed_phase"], "engine-verify")

    def test_gate_without_observed_rows_uses_the_generic_forms(self) -> None:
        code, out, _, _, _ = self.run_install(FakeHost(), FakeRunners(code=5, failing="engine-verify"))

        self.assertEqual(code, 1)
        self.assertIn("engine-verify: refuse — engine verify refused (exit 5)", out)
        self.assertNotIn(install.ENGINE_VERIFY_SKIPPED_DETAIL, out)

    def test_stops_at_the_first_failed_phase_and_names_the_command(self) -> None:
        for failing, command in (("reconcile", "users reconcile --now"), ("drill", "backup drill")):
            with self.subTest(failing=failing):
                code, out, _, runners, backend = self.run_install(
                    FakeHost(), FakeRunners(failing=failing)
                )
                self.assertEqual(code, 1)
                expected = PHASES[: PHASES.index(failing) + 1] + ["audit"]
                self.assertEqual(row_names(out), expected)
                self.assertIn(
                    f"{failing}: refuse — {command} refused (exit 3) Fix: Run sudo python3 -m "
                    f"gideon {command}, correct its refusal, then re-run install.",
                    out,
                )
                self.assertEqual(runners.calls[-1][0], failing)
                self.assertNotIn("https://", out.splitlines()[-1])
                self.assertEqual([row.detail["phase"] for row in backend.rows], ["intent", "failed"])
                self.assertEqual(backend.rows[-1].detail["failed_phase"], failing)

    def test_a_failure_before_the_stores_exist_leaves_no_row(self) -> None:
        for failing in ("preflight", "apply"):
            with self.subTest(failing=failing):
                code, out, _, runners, backend = self.run_install(
                    FakeHost(), FakeRunners(failing=failing)
                )
                self.assertEqual(code, 1)
                self.assertEqual(row_names(out), PHASES[: PHASES.index(failing) + 1])
                self.assertEqual([name for name, _ in runners.calls][-1], failing)
                self.assertEqual(backend.rows, [])
                self.assertEqual(backend.writes, 0)

    def test_a_failed_audit_write_fails_the_run(self) -> None:
        for fail_from, expected_rows, last_runner in (
            (0, ["preflight", "apply", "audit"], "apply"),
            (1, PHASES[:6] + ["audit"], "drill"),
        ):
            with self.subTest(fail_from=fail_from):
                code, out, _, runners, _ = self.run_install(
                    FakeHost(), audit_backend=FakeAudit(fail_from=fail_from)
                )
                self.assertEqual(code, 1)
                self.assertEqual(row_names(out), expected_rows)
                self.assertIn("audit: refuse — ", out)
                self.assertIn("Fix: Run sudo python3 -m gideon apply, then retry install.", out)
                self.assertEqual(runners.calls[-1][0], last_runner)
                self.assertNotIn("https://", out.splitlines()[-1])

    def test_root_and_site_refusals_run_nothing(self) -> None:
        cases = (
            (FakeHost(euid=1000), "gideon install: root is required. Fix: Run sudo python3 -m gideon install, then retry."),
            (FakeHost(site=False), "Fix: Correct /etc/gideon/site.yaml, then retry install."),
        )
        for host, expected in cases:
            with self.subTest(expected=expected):
                code, out, err, runners, backend = self.run_install(host)
                self.assertEqual(code, 1)
                self.assertIn(expected, err)
                self.assertEqual(out, "")
                self.assertEqual(runners.calls, [])
                self.assertEqual(backend.rows, [])


class ModuleShape(unittest.TestCase):
    def test_install_has_no_function_level_import(self) -> None:
        # The old process must load nothing after an upgrade's checkout moves the
        # tree under it; the same rule binds install, which upgrade's shape follows.
        tree = ast.parse((ROOT / "gideon/host/install.py").read_text())
        nested = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body
        ]
        self.assertEqual(nested, [])
