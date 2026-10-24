"""Contracts for the CI sibling stack tool."""

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

from test_render import artifact_source_files
from test_render_ci import inputs

from gideon.host import nogpu, owui
from gideon.host import stack as host_stack
from gideon.host.render import ARTIFACTS, RenderInputs
from gideon.host.render.api import API_INSTRUCTION_PATH, API_SERVICE_NAME
from gideon.host.render.ci import (
    CI_OPENSEARCH_DATA_ROOT,
    CI_OPENSEARCH_RENDERED_DIR,
    CI_PORT,
    CI_QDRANT_DATA_ROOT,
    CI_ROOT,
    CI_SECRETS_DIR,
    CI_SKIPPED_SECRETS,
    CI_WIPE_PATHS,
    CI_WORK_ROOT,
    ci_compose_document,
    ci_instruction,
)
from gideon.host.render.compose import (
    INTERNAL_NETWORK_NAME,
    container_name,
    network_name,
)
from gideon.host.render.engine import (
    EMBED_SECRET_NAME,
    ENGINE_SECRET_NAME,
    INTEGRATION_NETWORK_NAME,
)
from gideon.host.render.opensearch import (
    OPENSEARCH_GID,
    OPENSEARCH_SERVICE_NAME,
    OPENSEARCH_UID,
)
from gideon.host.render.searxng import SEARXNG_SECRET_NAME
from gideon.host.render.worker import DIR_MODE
from gideon.host.report import StageResult
from gideon.host.secrets import EnsureResult
from gideon.host.sysio import Command, PathLike
from tools.cistack import cli
from tools.cistack import run as cistack_run

ROOT = Path(__file__).resolve().parents[1]
SITE_PATH = Path("/etc/gideon/site.yaml")
TEMPLATE_PATHS = tuple(
    dict.fromkeys(path for artifact in ARTIFACTS for path in artifact.template_paths)
)


def completed(
    argv: Sequence[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


class FakeHost:
    """A dict-backed Host whose command results are explicit and inspectable."""

    def __init__(self, *, euid: int = 0, network_returncode: int = 0) -> None:
        self.euid = euid
        self.network_returncode = network_returncode
        self.network_results: dict[str, int | OSError | subprocess.CompletedProcess[str]] = {}
        self.network_containers: dict[str, dict[str, object]] = {}
        self.job_id: int | None = None
        self.job_returncode = 0
        self.job_read_locks: list[bool] = []
        self.engine_lock_reads: list[tuple[str, bool]] = []
        self.failing_programs: set[str] = set()
        self.events: list[tuple[str, str]] = []
        self.files: dict[str, str] = {
            cistack_run.backuplock.LOCK_TABLE: "",
            os.fspath(SITE_PATH): (ROOT / "config/site.example.yaml").read_text(
                encoding="utf-8"
            ),
        }
        self.stats: dict[str, os.stat_result | OSError] = {}
        self.read_errors: dict[str, OSError] = {}
        self.directories: set[str] = {CI_ROOT, CI_SECRETS_DIR, cistack_run.backuplock.LOCK_DIR}
        self.commands: list[tuple[str, ...]] = []
        self.locks: dict[str, str] = {}
        self.lock_events: list[tuple[str, str]] = []
        self.writes: list[tuple[str, int]] = []
        self.mkdirs: list[tuple[str, int]] = []
        self.chowns: list[tuple[str, int, int]] = []
        self.chmods: list[tuple[str, int]] = []
        self._seed_checkout()

    def _seed_checkout(self) -> None:
        for relative in ("host.lock", "images.lock", "models.lock"):
            path = ROOT / relative
            self.files[os.fspath(path)] = path.read_text(encoding="utf-8")
        self.files.update(artifact_source_files())

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
        del check, cwd, env, timeout, passthrough
        command = tuple(argv)
        self.commands.append(command)
        self.engine_lock_reads.append(
            (command[0], cistack_run.backuplock.ENGINE_LOCK.path in self.locks)
        )
        if command[0] in self.failing_programs:
            return completed(command, returncode=1, stderr="fixture failure\n")
        if command[:3] == ("docker", "network", "inspect"):
            network = command[3]
            outcome = self.network_results.get(network, self.network_returncode)
            if isinstance(outcome, OSError):
                raise outcome
            if isinstance(outcome, subprocess.CompletedProcess):
                return outcome
            return completed(
                command,
                returncode=outcome,
                stdout=json.dumps([{"Containers": self.network_containers.get(network, {})}]) + "\n",
                stderr="network unavailable\n" if outcome else "",
            )
        if input is not None and "FROM procrastinate_jobs" in input:
            self.events.append(("read-job", ""))
            self.job_read_locks.append(cistack_run.backuplock.ENGINE_LOCK.path in self.locks)
            return completed(
                command,
                returncode=self.job_returncode,
                stdout=f"{self.job_id}\n" if self.job_id is not None else "",
                stderr="record unavailable\n" if self.job_returncode else "",
            )
        if command[:2] == ("docker", "compose") and "ps" in command:
            rows = "\n".join(
                f'{{"Service":"{name}","State":"running","Health":"healthy"}}'
                for name in cistack_run.CI_SERVICES
            )
            return completed(command, stdout=rows + "\n")
        if command and command[0] == "rm" and len(command) == 3:
            self.remove(command[2])
        if command[:2] == ("getent", "group"):
            return completed(command, stdout="gideon:x:1001:\n")
        return completed(command)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key in self.read_errors:
            raise self.read_errors[key]
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding
        key = os.fspath(path)
        self.files[key] = text
        self.directories.add(os.path.dirname(key))
        self.writes.append((key, mode))
        self.events.append(("write", key))

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        self.engine_lock_reads.append(("exists", cistack_run.backuplock.ENGINE_LOCK.path in self.locks))
        return key in self.files or key in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        key = os.fspath(path)
        if not self.exists(key):
            raise FileNotFoundError(key)
        prefix = key.rstrip("/") + "/"
        children: set[str] = set()
        for candidate in (*self.files, *self.directories):
            if not candidate.startswith(prefix):
                continue
            remainder = candidate.removeprefix(prefix)
            if remainder and "/" not in remainder:
                children.add(remainder)
        return sorted(children)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        key = os.fspath(path)
        if key in self.stats:
            value = self.stats[key]
            if isinstance(value, OSError):
                raise value
            return value
        if not self.exists(path):
            raise FileNotFoundError(os.fspath(path))
        return os.stat_result((0o040755 if os.fspath(path) in self.directories else 0o100644,))

    def hold_backup_lock(self, record: str) -> None:
        path = cistack_run.backuplock.BACKUP_LOCK.path
        info = os.stat_result((0o100600, 24680, os.makedev(0xAB, 0xCD), 1, 0, 0, 0, 0, 0, 0))
        self.stats[path] = info
        self.files[path] = record
        self.locks[path] = record
        self.files[cistack_run.backuplock.LOCK_TABLE] = (
            f"1: FLOCK ADVISORY WRITE 12345 "
            f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n"
        )

    def chmod(self, path: PathLike, mode: int) -> None:
        self.chmods.append((os.fspath(path), mode))
        self.events.append(("chmod", os.fspath(path)))

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.chowns.append((os.fspath(path), uid, gid))
        self.events.append(("chown", os.fspath(path)))

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del parents, exist_ok
        key = os.fspath(path)
        self.directories.add(key)
        self.mkdirs.append((key, mode))
        self.events.append(("mkdir", key))

    def remove(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        self.files = {
            key: value
            for key, value in self.files.items()
            if key != path and not key.startswith(prefix)
        }
        self.directories = {
            key for key in self.directories if key != path and not key.startswith(prefix)
        }

    def geteuid(self) -> int:
        return self.euid

    def take_lock(self, path: PathLike, record: str) -> str | None:
        key = os.fspath(path)
        self.lock_events.append(("take", key))
        if key in self.locks:
            return self.locks[key]
        self.locks[key] = record
        self.events.append(("take-lock", key))
        return None

    def release_lock(self, path: PathLike) -> None:
        key = os.fspath(path)
        self.lock_events.append(("release", key))
        self.locks.pop(key, None)
        self.events.append(("release-lock", key))


def ci_stack() -> cistack_run.CiStack:
    return cistack_run.CiStack(
        project_dir=Path(CI_ROOT),
        secrets_dir=Path(CI_SECRETS_DIR),
        base_url=f"http://127.0.0.1:{CI_PORT}",
        checkout=ROOT,
    )


class Preconditions(unittest.TestCase):
    def result(self, host: FakeHost) -> StageResult:
        return cistack_run._preconditions(ci_stack(), host, site_path=SITE_PATH)

    def test_available_production_networks_are_named_in_the_row(self) -> None:
        host = FakeHost()

        result = self.result(host)

        self.assertTrue(result.ok)
        for key in (INTEGRATION_NETWORK_NAME, INTERNAL_NETWORK_NAME):
            self.assertIn(f"{key} network", result.detail)
        self.assertEqual(
            host.commands,
            [("docker", "network", "inspect", network_name(key))
             for key in (INTEGRATION_NETWORK_NAME, INTERNAL_NETWORK_NAME)],
        )

    def test_each_production_network_refusal_names_the_network_and_apply_fix(self) -> None:
        for key in (INTEGRATION_NETWORK_NAME, INTERNAL_NETWORK_NAME):
            for outcome in (1, OSError("docker unavailable")):
                with self.subTest(network=key, outcome=outcome):
                    host = FakeHost()
                    host.network_results[network_name(key)] = outcome

                    result = self.result(host)

                    self.assertFalse(result.ok)
                    self.assertEqual(result.name, "preconditions")
                    self.assertIn(f"{key} Docker network", result.detail)
                    self.assertEqual(result.fix, "Run sudo python3 -m gideon apply, then retry.")
                    self.assertEqual(host.commands[-1], ("docker", "network", "inspect", network_name(key)))
                    self.assertEqual(host.writes, [])

    def test_refusal_matrix_names_the_fix_and_stops_at_preconditions(self) -> None:
        cases = (
            ("root", FakeHost(euid=1000), "tools.cistack up"),
            ("no GPU", self._no_gpu_host(), "no-gpu"),
            ("data root", self._missing_data_host(), "host provision"),
            ("network", FakeHost(network_returncode=1), "gideon apply"),
        )
        for name, host, expected in cases:
            with self.subTest(name=name):
                result = self.result(host)
                self.assertFalse(result.ok)
                self.assertIn(expected, result.fix)
                self.assertEqual(result.name, "preconditions")
                if name == "network":
                    self.assertIn("integration Docker network", result.detail)
                    self.assertEqual(result.fix, "Run sudo python3 -m gideon apply, then retry.")

    @staticmethod
    def _no_gpu_host() -> FakeHost:
        host = FakeHost()
        host.files[os.fspath(nogpu.NO_GPU_PATH)] = "declared\n"
        return host

    @staticmethod
    def _missing_data_host() -> FakeHost:
        host = FakeHost()
        host.directories.remove(CI_ROOT)
        return host

    def test_foreign_render_file_refuses_before_writing(self) -> None:
        host = FakeHost()
        foreign = f"{CI_ROOT}/notes.txt"
        host.files[foreign] = "hand-written\n"
        fake_inputs = cast(RenderInputs, object())
        with (
            patch.object(cistack_run, "ci_compose_document", return_value={"services": {}}),
            patch.object(cistack_run, "ci_env_file", return_value=""),
            patch.object(cistack_run, "ci_manifest", return_value=""),
        ):
            result = cistack_run._render_stage(ci_stack(), host, fake_inputs)
        self.assertFalse(result.ok)
        self.assertIn("notes.txt", result.detail)
        self.assertEqual(host.writes, [])

    def test_foreign_file_in_the_api_directory_refuses_before_writing(self) -> None:
        host = FakeHost()
        host.directories.add(f"{CI_ROOT}/{API_SERVICE_NAME}")
        host.files[f"{CI_ROOT}/{API_INSTRUCTION_PATH}"] = "fixture instruction\n"
        host.files[f"{CI_ROOT}/{API_SERVICE_NAME}/notes.txt"] = "hand-written\n"

        result = cistack_run._render_stage(ci_stack(), host, inputs())

        self.assertFalse(result.ok)
        self.assertIn(f"{API_SERVICE_NAME}/notes.txt", result.detail)
        self.assertNotIn(API_INSTRUCTION_PATH, result.detail)
        self.assertEqual(host.writes, [])

    def test_foreign_file_in_opensearch_directories_refuses_before_writing(self) -> None:
        for relative in ("opensearch/notes.txt", "opensearch/security/notes.txt"):
            with self.subTest(path=relative):
                host = FakeHost()
                host.directories.update(
                    {CI_OPENSEARCH_RENDERED_DIR, f"{CI_OPENSEARCH_RENDERED_DIR}/security"}
                )
                host.files[f"{CI_ROOT}/{relative}"] = "hand-written\n"

                result = cistack_run._render_stage(ci_stack(), host, inputs())

                self.assertFalse(result.ok)
                self.assertEqual(result.name, "render")
                self.assertIn(relative, result.detail)
                self.assertEqual(host.writes, [])

    def test_render_writes_store_files_and_creates_data_roots(self) -> None:
        host = FakeHost()
        render_inputs = inputs()
        opensearch_artifacts = tuple(
            artifact
            for artifact in ARTIFACTS
            if OPENSEARCH_SERVICE_NAME in artifact.owners
        )

        result = cistack_run._render_stage(ci_stack(), host, render_inputs)

        self.assertTrue(result.ok)
        self.assertIn(f"{CI_ROOT}/{API_SERVICE_NAME}", host.directories)
        self.assertIn("API instruction", result.detail)
        self.assertIn("OpenSearch settings and security files", result.detail)
        self.assertIn("Qdrant and OpenSearch data roots", result.detail)
        self.assertEqual(len(opensearch_artifacts), 7)
        self.assertEqual(
            host.writes,
            [
                (f"{CI_ROOT}/compose.yaml", 0o644),
                (f"{CI_ROOT}/open-webui/env", 0o600),
                (f"{CI_ROOT}/open-webui/manifest.yaml", 0o644),
                (f"{CI_ROOT}/{API_INSTRUCTION_PATH}", 0o644),
            ]
            + [
                (f"{CI_ROOT}/{artifact.relative_path}", 0o644)
                for artifact in opensearch_artifacts
            ],
        )
        for artifact in opensearch_artifacts:
            with self.subTest(artifact=artifact.name):
                self.assertEqual(artifact.mode, 0o644)
                self.assertEqual(
                    host.files[f"{CI_ROOT}/{artifact.relative_path}"],
                    artifact.emit(render_inputs),
                )
        self.assertEqual(
            host.files[f"{CI_ROOT}/{API_INSTRUCTION_PATH}"],
            ci_instruction(render_inputs),
        )
        self.assertIn((CI_QDRANT_DATA_ROOT, 0o750), host.mkdirs)
        self.assertIn((CI_OPENSEARCH_DATA_ROOT, 0o700), host.mkdirs)
        self.assertIn((CI_OPENSEARCH_RENDERED_DIR, 0o755), host.mkdirs)
        self.assertIn((f"{CI_OPENSEARCH_RENDERED_DIR}/security", 0o755), host.mkdirs)
        service_gid = cistack_run.secrets.service_group_gid(host)
        assert service_gid is not None
        self.assertIn((CI_QDRANT_DATA_ROOT, 0, service_gid), host.chowns)
        self.assertIn((CI_OPENSEARCH_DATA_ROOT, OPENSEARCH_UID, OPENSEARCH_GID), host.chowns)
        self.assertEqual([entry for entry in host.mkdirs if entry[0] == CI_WORK_ROOT], [(CI_WORK_ROOT, DIR_MODE)])
        self.assertEqual([entry for entry in host.chowns if entry[0] == CI_WORK_ROOT], [(CI_WORK_ROOT, 0, service_gid)])
        self.assertEqual([entry for entry in host.chmods if entry[0] == CI_WORK_ROOT], [(CI_WORK_ROOT, DIR_MODE)])
        host.writes.clear()
        self.assertTrue(cistack_run._render_stage(ci_stack(), host, render_inputs).ok)
        self.assertEqual(len(host.writes), 4 + len(opensearch_artifacts))
        self.assertEqual([entry for entry in host.mkdirs if entry[0] == CI_WORK_ROOT], [(CI_WORK_ROOT, DIR_MODE)])
        self.assertEqual([entry for entry in host.chowns if entry[0] == CI_WORK_ROOT], [(CI_WORK_ROOT, 0, service_gid)])
        self.assertEqual([entry for entry in host.chmods if entry[0] == CI_WORK_ROOT], [(CI_WORK_ROOT, DIR_MODE)])

    def test_render_leaves_existing_data_roots_untouched(self) -> None:
        host = FakeHost()
        roots = {CI_QDRANT_DATA_ROOT, CI_OPENSEARCH_DATA_ROOT, CI_WORK_ROOT}
        host.directories.update(roots)

        result = cistack_run._render_stage(ci_stack(), host, inputs())

        self.assertTrue(result.ok)
        self.assertFalse(any(path in roots for path, _ in host.mkdirs))
        self.assertFalse(any(path in roots for path, _, _ in host.chowns))
        self.assertFalse(any(path in roots for path, _ in host.chmods))


class SmokeCommand(unittest.TestCase):
    def test_backup_lock_skips_without_commands_or_a_take(self) -> None:
        record = cistack_run.backuplock.Record(
            "fixture restore", 12345, datetime(2026, 1, 1, tzinfo=UTC),
        )
        for text in (record.to_json(), "unreadable fixture record"):
            with self.subTest(record=text):
                host = FakeHost()
                host.hold_backup_lock(text)
                before_files = host.files.copy()
                before_directories = host.directories.copy()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cistack_run.smoke(
                        ci_stack(), host, site_path=SITE_PATH,
                        run_eval=lambda *args, **kwargs: self.fail("evaluated a held box"),
                    )

                holder = (
                    f"{record.command} since {record.started.isoformat()} (pid {record.pid})"
                    if text == record.to_json()
                    else "another gideon command, its record unreadable"
                )
                self.assertEqual(code, 0)
                self.assertEqual(output.getvalue().splitlines(), [
                    "preconditions: ok — engine lock taken",
                    f"skipped: ok — the backup lock is held by {holder}; the box is held, "
                    "the sibling untouched, nothing evaluated, nothing recorded",
                ])
                self.assertEqual(host.commands, [])
                self.assertEqual(host.writes, [])
                self.assertEqual(host.files, before_files)
                self.assertEqual(host.directories, before_directories)
                engine_path = cistack_run.backuplock.ENGINE_LOCK.path
                self.assertEqual(host.lock_events, [("take", engine_path), ("release", engine_path)])
                self.assertEqual(host.locks, {cistack_run.backuplock.BACKUP_LOCK.path: text})

    def test_backup_read_errors_refuse_and_release_before_any_command(self) -> None:
        for source in ("stat", "table", "record"):
            with self.subTest(source=source):
                host = FakeHost()
                host.hold_backup_lock("fixture record")
                path = cistack_run.backuplock.BACKUP_LOCK.path
                error = OSError("fixture lock read failure")
                if source == "stat":
                    host.stats[path] = error
                else:
                    host.read_errors[
                        cistack_run.backuplock.LOCK_TABLE if source == "table" else path
                    ] = error
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cistack_run.smoke(
                        ci_stack(), host, site_path=SITE_PATH,
                        run_eval=lambda *args, **kwargs: self.fail("evaluated after a read failure"),
                    )
                self.assertEqual(code, 1)
                self.assertEqual(output.getvalue().splitlines(), [
                    "preconditions: refuse — the backup lock could not be read: fixture lock read failure "
                    "Fix: Repair access to the backup lock and the kernel lock table, then retry.",
                ])
                self.assertEqual(host.commands, [])
                self.assertEqual(host.writes, [])
                engine_path = cistack_run.backuplock.ENGINE_LOCK.path
                self.assertEqual(host.lock_events, [("take", engine_path), ("release", engine_path)])
                self.assertNotIn(engine_path, host.locks)

    def test_absent_network_skips_smoke_but_up_keeps_its_refusal(self) -> None:
        for verb in ("smoke", "up"):
            for key in (INTEGRATION_NETWORK_NAME, INTERNAL_NETWORK_NAME):
                with self.subTest(verb=verb, network=key):
                    host = FakeHost()
                    network = network_name(key)
                    host.network_results[network] = completed(
                        ["docker", "network", "inspect", network], returncode=1,
                        stderr=f"Error response from daemon: network {network} not found\n",
                    )
                    before_files = host.files.copy()
                    before_directories = host.directories.copy()
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        if verb == "smoke":
                            code = cistack_run.smoke(
                                ci_stack(), host, site_path=SITE_PATH,
                                run_eval=lambda *args, **kwargs: self.fail("evaluated a down stack"),
                            )
                        else:
                            code = cistack_run.up(ci_stack(), host, site_path=SITE_PATH)

                    if verb == "smoke":
                        self.assertEqual(code, 0)
                        self.assertEqual(output.getvalue().splitlines(), [
                            "preconditions: ok — engine lock taken",
                            f"skipped: ok — the production stack is down: the {key} Docker network is absent; "
                            "the sibling untouched, nothing evaluated, nothing recorded",
                        ])
                    else:
                        self.assertEqual(code, 1)
                        self.assertEqual(output.getvalue().splitlines(), [
                            f"preconditions: refuse — {key} Docker network is unavailable: "
                            f"Error response from daemon: network {network} not found "
                            "Fix: Run sudo python3 -m gideon apply, then retry.",
                        ])
                    self.assertEqual(host.commands, [
                        ("docker", "network", "inspect", network_name(name))
                        for name in (INTEGRATION_NETWORK_NAME, INTERNAL_NETWORK_NAME)
                        if name == INTEGRATION_NETWORK_NAME or key == INTERNAL_NETWORK_NAME
                    ])
                    self.assertEqual(host.files, before_files)
                    self.assertEqual(host.directories, before_directories)
                    self.assertEqual(host.writes, [])
                    engine_path = cistack_run.backuplock.ENGINE_LOCK.path
                    self.assertEqual(host.lock_events, [("take", engine_path), ("release", engine_path)])
                    self.assertEqual(host.locks, {})

    def test_other_network_failures_refuse_smoke_with_the_apply_fix(self) -> None:
        for key in (INTEGRATION_NETWORK_NAME, INTERNAL_NETWORK_NAME):
            network = network_name(key)
            failures = (
                (1, f"{key} Docker network is unavailable: network unavailable | "
                 + json.dumps([{"Containers": {}}])),
                (OSError("docker unavailable"), f"{key} Docker network could not be inspected: docker unavailable"),
                (
                    completed(
                        ["docker", "network", "inspect", network], returncode=1,
                        stderr="network fixture-unrelated not found\n",
                    ),
                    f"{key} Docker network is unavailable: network fixture-unrelated not found",
                ),
            )
            for outcome, detail in failures:
                with self.subTest(network=key, outcome=outcome):
                    host = FakeHost()
                    host.network_results[network] = outcome
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = cistack_run.smoke(
                            ci_stack(), host, site_path=SITE_PATH,
                            run_eval=lambda *args, **kwargs: self.fail("evaluated after a network failure"),
                        )
                    self.assertEqual(code, 1)
                    self.assertEqual(output.getvalue().splitlines(), [
                        f"preconditions: refuse — {detail} Fix: Run sudo python3 -m gideon apply, then retry.",
                    ])
                    self.assertEqual(host.writes, [])
                    engine_path = cistack_run.backuplock.ENGINE_LOCK.path
                    self.assertEqual(host.lock_events, [("take", engine_path), ("release", engine_path)])
                    self.assertEqual(host.locks, {})

    def test_first_skip_wins_when_multiple_conditions_hold(self) -> None:
        for engine, backup, job, reason in (
            (True, True, True, "engine busy"),
            (False, True, False, "the backup lock is held"),
            (False, True, True, "the backup lock is held"),
            (False, False, True, "a sample build is running"),
        ):
            with self.subTest(engine=engine, backup=backup, job=job):
                host = FakeHost()
                network = network_name(INTEGRATION_NETWORK_NAME)
                host.network_results[network] = completed(
                    ["docker", "network", "inspect", network], returncode=1,
                    stderr=f"network {network} not found\n",
                )
                host.files[f"{CI_ROOT}/compose.yaml"] = "services: {}\n"
                if backup:
                    host.hold_backup_lock("unreadable fixture record")
                engine_path = cistack_run.backuplock.ENGINE_LOCK.path
                if engine:
                    host.locks[engine_path] = cistack_run.backuplock.Record(
                        "fixture evaluation", os.getpid() + 1, datetime(2026, 1, 1, tzinfo=UTC),
                    ).to_json()
                if job:
                    host.job_id = 12345
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cistack_run.smoke(
                        ci_stack(), host, site_path=SITE_PATH,
                        run_eval=lambda *args, **kwargs: self.fail("evaluated despite a skip"),
                    )
                self.assertEqual(code, 0)
                self.assertEqual(len(output.getvalue().splitlines()), 2)
                self.assertIn(f"skipped: ok — {reason}", output.getvalue())
                self.assertEqual(host.job_read_locks, [] if engine or backup else [True])
                self.assertEqual(len(host.commands), 0 if engine or backup else 1)
                self.assertFalse(any(command[:3] == ("docker", "network", "inspect") for command in host.commands))
                self.assertEqual(host.writes, [])
                self.assertNotIn(("take", cistack_run.backuplock.BACKUP_LOCK.path), host.lock_events)

    def test_smoke_inspects_once_and_up_keeps_its_rows_with_a_backup_hold(self) -> None:
        outputs: list[list[str]] = []
        evaluated: list[bool] = []

        def run_eval(*args: object, **kwargs: object) -> int:
            evaluated.append(True)
            return 0

        success = StageResult("fixture-stage", True, "fixture ready", "")
        with (
            patch.object(cistack_run, "_secrets_stage", return_value=(success, cast(RenderInputs, object()))),
            patch.object(cistack_run, "_render_stage", return_value=success),
            patch.object(cistack_run, "_stores_stage", return_value=success),
            patch.object(cistack_run, "_start_stage", return_value=success),
            patch.object(cistack_run, "_manifest_stage", return_value=success),
            patch.object(cistack_run, "_verify_stage", return_value=success),
        ):
            for verb, backup_held in (("up", False), ("up", True), ("smoke", False)):
                with self.subTest(verb=verb, backup_held=backup_held):
                    host = FakeHost()
                    host.files[f"{CI_ROOT}/compose.yaml"] = "services: {}\n"
                    if backup_held:
                        host.hold_backup_lock("unreadable fixture record")
                        host.read_errors[cistack_run.backuplock.LOCK_TABLE] = OSError("must not read")
                    if verb == "smoke":
                        network = network_name(INTEGRATION_NETWORK_NAME)
                        host.network_results[network] = completed(
                            ["docker", "network", "inspect", network],
                            stderr=f"network {network} not found\n",
                        )
                    evaluated.clear()
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        if verb == "smoke":
                            code = cistack_run.smoke(
                                ci_stack(), host, site_path=SITE_PATH, run_eval=run_eval,
                            )
                        else:
                            code = cistack_run.up(ci_stack(), host, site_path=SITE_PATH)
                    self.assertEqual(code, 0)
                    rows = output.getvalue().splitlines()
                    outputs.append(rows)
                    self.assertEqual(rows[0],
                        "preconditions: ok — root, render inputs, CI data root, integration network, "
                        "and internal network are available; engine lock taken",
                    )
                    self.assertEqual(evaluated, [True] if verb == "smoke" else [])
                    self.assertEqual(host.job_read_locks, [True])
                    self.assertEqual(
                        [command for command in host.commands if command[:3] == ("docker", "network", "inspect")],
                        [("docker", "network", "inspect", network_name(key))
                         for key in (INTEGRATION_NETWORK_NAME, INTERNAL_NETWORK_NAME)],
                    )
                    engine_path = cistack_run.backuplock.ENGINE_LOCK.path
                    self.assertEqual(host.lock_events, [("take", engine_path), ("release", engine_path)])
                    self.assertNotIn(engine_path, host.locks)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[2][:-1], outputs[0])
        self.assertEqual(outputs[2][-1], "smoke: ok — eval run --slice smoke --stack ci --kind smoke exited 0")

    def test_busy_engine_skips_without_running_commands_or_touching_the_sibling(self) -> None:
        host = FakeHost()
        lock_path = cistack_run.backuplock.ENGINE_LOCK.path
        host.locks[lock_path] = json.dumps(
            {
                "command": "eval run --slice general-smoke",
                "pid": os.getpid() + 1,
                "started": "2026-01-01T00:00:00+00:00",
            }
        )
        output = io.StringIO()
        with (
            patch.object(cistack_run, "_up_stages") as up_stages,
            contextlib.redirect_stdout(output),
        ):
            code = cli.main(
                ["smoke"], host=host, checkout=ROOT, site_path=SITE_PATH,
                run_eval=lambda *args, **kwargs: self.fail("run_eval called while busy"),
            )

        self.assertEqual(code, 0)
        self.assertIn("eval run --slice general-smoke", output.getvalue())
        self.assertIn("skipped: ok", output.getvalue())
        self.assertEqual(host.commands, [])
        self.assertEqual(host.writes, [])
        up_stages.assert_not_called()

    def test_live_sample_job_skips_even_when_an_interrupted_builder_released_its_lock(self) -> None:
        """A queued or running job outlives its client's interrupted wait."""
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                host = FakeHost()
                host.files[f"{CI_ROOT}/compose.yaml"] = "services: {}\n"
                lock_path = cistack_run.backuplock.ENGINE_LOCK.path
                if interrupted:
                    host.take_lock(lock_path, json.dumps({
                        "command": "index build caselaw --sample example-audit",
                        "pid": os.getpid(),
                        "started": "2026-01-01T00:00:00+00:00",
                    }))
                host.job_id = 12345
                if interrupted:
                    host.release_lock(lock_path)
                host.events.clear()
                before_files = host.files.copy()
                before_directories = host.directories.copy()
                output = io.StringIO()

                with contextlib.redirect_stdout(output):
                    code = cistack_run.smoke(
                        ci_stack(), host, site_path=SITE_PATH, sleep=lambda _: None,
                        run_eval=lambda *args, **kwargs: self.fail("evaluated a live build"),
                    )

                self.assertEqual(code, 0)
                self.assertEqual(output.getvalue().splitlines(), [
                    "preconditions: ok — engine lock taken",
                    "skipped: ok — a sample build is running on the sibling (job 12345); "
                    "the sibling untouched, nothing evaluated, nothing recorded",
                ])
                self.assertEqual(host.job_read_locks, [True])
                self.assertEqual(
                    [event for event in host.events if event[1] != cistack_run.backuplock.LOCK_DIR],
                    [("take-lock", lock_path), ("read-job", ""), ("release-lock", lock_path)],
                )
                self.assertEqual(host.files, before_files)
                self.assertEqual(host.directories, before_directories)
                self.assertEqual(len(host.commands), 1)
                self.assertIn("exec", host.commands[0])
                self.assertNotIn(lock_path, host.locks)

    def test_up_refuses_a_live_sample_job_with_a_wait_or_rejoin_fix(self) -> None:
        """A convergence never recreates the containers of a running build."""
        host = FakeHost()
        host.files[f"{CI_ROOT}/compose.yaml"] = "services: {}\n"
        host.job_id = 12345
        before_files = host.files.copy()
        before_directories = host.directories.copy()
        lock_path = cistack_run.backuplock.ENGINE_LOCK.path
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = cistack_run.up(ci_stack(), host, site_path=SITE_PATH, sleep=lambda _: None)

        self.assertEqual(code, 1)
        detail, fix = output.getvalue().strip().split(" Fix: ")
        self.assertEqual(detail, "preconditions: refuse — a sample build is running on the sibling (job 12345)")
        self.assertIn("Wait for the build's job or rejoin it", fix)
        self.assertIn("index build caselaw --sample", fix)
        self.assertEqual(host.job_read_locks, [True])
        self.assertEqual(
            [event for event in host.events if event[1] != cistack_run.backuplock.LOCK_DIR],
            [("take-lock", lock_path), ("read-job", ""), ("release-lock", lock_path)],
        )
        self.assertEqual(host.files, before_files)
        self.assertEqual(host.directories, before_directories)
        self.assertEqual(len(host.commands), 1)
        self.assertIn("exec", host.commands[0])
        self.assertNotIn(lock_path, host.locks)

    def test_failed_live_job_read_refuses_up_and_smoke_before_touching_the_sibling(self) -> None:
        """An unreadable queue cannot show that a convergence is safe."""
        for verb in ("up", "smoke"):
            with self.subTest(verb=verb):
                host = FakeHost()
                host.files[f"{CI_ROOT}/compose.yaml"] = "services: {}\n"
                host.job_returncode = 2
                before_files = host.files.copy()
                before_directories = host.directories.copy()
                output = io.StringIO()
                action = cistack_run.up if verb == "up" else cistack_run.smoke

                with contextlib.redirect_stdout(output):
                    code = action(ci_stack(), host, site_path=SITE_PATH, sleep=lambda _: None)

                self.assertEqual(code, 1)
                detail, fix = output.getvalue().strip().split(" Fix: ")
                self.assertEqual(detail, "preconditions: refuse — index job query failed (exit 2)")
                self.assertIn(host_stack.logs_fix(CI_ROOT, "postgres"), fix)
                self.assertIn(f"sudo python3 -m tools.cistack {verb}", fix)
                self.assertEqual(host.job_read_locks, [True])
                lock_path = cistack_run.backuplock.ENGINE_LOCK.path
                self.assertEqual(
                    [event for event in host.events if event[1] != cistack_run.backuplock.LOCK_DIR],
                    [("take-lock", lock_path), ("read-job", ""), ("release-lock", lock_path)],
                )
                self.assertEqual(host.files, before_files)
                self.assertEqual(host.directories, before_directories)
                self.assertEqual(len(host.commands), 1)
                self.assertNotIn(lock_path, host.locks)

    def test_smoke_converges_then_calls_eval_with_selected_checkout(self) -> None:
        host = FakeHost()
        events: list[str] = []
        evaluated: list[tuple[object, dict[str, object]]] = []

        def up_stages(*args: object, **kwargs: object) -> int:
            del args, kwargs
            events.append("up")
            return 0

        def run_eval(args: object, **kwargs: object) -> int:
            events.append("smoke")
            evaluated.append((args, kwargs))
            return 0

        selected = ROOT / "selected-checkout"
        with (
            patch.object(cistack_run, "_up_stages", side_effect=up_stages),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = cli.main(
                ["smoke", "--checkout", str(selected)],
                host=host,
                checkout=ROOT,
                site_path=SITE_PATH,
                run_eval=run_eval,
            )

        self.assertEqual(code, 0)
        self.assertEqual(events, ["up", "smoke"])
        self.assertEqual(len(evaluated), 1)
        args, kwargs = evaluated[0]
        self.assertEqual(vars(args), {
            "slice": "smoke", "stack": "ci", "kind": "smoke", "set": None,
            "ranked": None, "decision": False, "force": False,
        })
        self.assertIs(kwargs["host"], host)
        self.assertEqual(kwargs["checkout_root"], selected)
        self.assertNotEqual(kwargs["checkout_root"], ROOT)
        self.assertEqual(
            host.lock_events,
            [("take", cistack_run.backuplock.ENGINE_LOCK.path),
             ("release", cistack_run.backuplock.ENGINE_LOCK.path)],
        )
        self.assertNotIn(cistack_run.backuplock.ENGINE_LOCK.path, host.locks)

    def test_failed_convergence_releases_the_lock_without_evaluating(self) -> None:
        host = FakeHost()
        with (
            patch.object(cistack_run, "_up_stages", return_value=1),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = cistack_run.smoke(
                ci_stack(), host, site_path=SITE_PATH,
                run_eval=lambda *args, **kwargs: self.fail("run_eval called after failed up"),
            )

        self.assertEqual(code, 1)
        self.assertEqual(host.lock_events[-1], ("release", cistack_run.backuplock.ENGINE_LOCK.path))
        self.assertNotIn(cistack_run.backuplock.ENGINE_LOCK.path, host.locks)

    def test_up_refuses_held_engine_lock_before_any_argv(self) -> None:
        host = FakeHost()
        lock_path = cistack_run.backuplock.ENGINE_LOCK.path
        host.locks[lock_path] = json.dumps(
            {
                "command": "tools.cistack smoke",
                "pid": os.getpid() + 1,
                "started": "2026-01-01T00:00:00+00:00",
            }
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cistack_run.up(ci_stack(), host, site_path=SITE_PATH)

        self.assertEqual(code, 1)
        self.assertIn("tools.cistack smoke", output.getvalue())
        self.assertIn("ps -p", output.getvalue())
        self.assertEqual(host.commands, [])


class Stages(unittest.TestCase):
    def test_service_list_matches_the_rendered_document_order(self) -> None:
        services = ci_compose_document(inputs())["services"]
        assert isinstance(services, Mapping)
        self.assertEqual(cistack_run.CI_SERVICES, tuple(services))
        self.assertEqual(len(cistack_run.CI_SERVICES), 9)

    def test_up_stage_order_stops_after_first_refusal(self) -> None:
        host = FakeHost()
        events: list[str] = []

        def preconditions(*args: object, **kwargs: object) -> StageResult:
            del args, kwargs
            events.append("preconditions")
            return StageResult("preconditions", True, "ready", "")

        def secrets_stage(*args: object, **kwargs: object) -> tuple[StageResult, RenderInputs | None]:
            del args, kwargs
            events.append("secrets")
            return StageResult("secrets", False, "refused", "fix"), None

        def render_stage(*args: object, **kwargs: object) -> StageResult:
            del args, kwargs
            events.append("render")
            return StageResult("render", True, "", "")

        with (
            patch.object(cistack_run, "_preconditions", preconditions),
            patch.object(cistack_run, "_secrets_stage", secrets_stage),
            patch.object(cistack_run, "_render_stage", render_stage),
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cistack_run.up(ci_stack(), host, site_path=SITE_PATH, sleep=lambda _: None)
        self.assertEqual(code, 1)
        self.assertEqual(events, ["preconditions", "secrets"])
        self.assertNotIn("render: ok", output.getvalue())

    def test_idempotent_up_reuses_the_same_ordered_stages(self) -> None:
        host = FakeHost()
        inputs = cast(RenderInputs, object())
        events: list[str] = []

        def record(name: str, result: StageResult) -> Callable[..., StageResult]:
            def stage(*args: object, **kwargs: object) -> StageResult:
                del args, kwargs
                events.append(name)
                return result

            return stage

        def secrets_stage(*args: object, **kwargs: object) -> tuple[StageResult, RenderInputs]:
            del args, kwargs
            events.append("secrets")
            return StageResult("secrets", True, "", ""), inputs

        with (
            patch.object(cistack_run, "_preconditions", record("preconditions", StageResult("preconditions", True, "", ""))),
            patch.object(cistack_run, "_secrets_stage", secrets_stage),
            patch.object(cistack_run, "_render_stage", record("render", StageResult("render", True, "", ""))),
            patch.object(cistack_run, "_stores_stage", record("stores", StageResult("stores", True, "", ""))),
            patch.object(cistack_run, "_start_stage", record("start", StageResult("start", True, "", ""))),
            patch.object(cistack_run, "_manifest_stage", record("apply-manifest", StageResult("apply-manifest", True, "", ""))),
            patch.object(cistack_run, "_verify_stage", record("verify", StageResult("verify", True, "", ""))),
        ):
            self.assertEqual(cistack_run.up(ci_stack(), host, site_path=SITE_PATH), 0)
            first = tuple(events)
            events.clear()
            self.assertEqual(cistack_run.up(ci_stack(), host, site_path=SITE_PATH), 0)
        self.assertEqual(tuple(events), first)
        self.assertEqual(
            first,
            ("preconditions", "secrets", "render", "stores", "start", "apply-manifest", "verify"),
        )

    def test_compose_and_exec_argv_use_the_ci_project_directory(self) -> None:
        host = FakeHost()
        with patch.object(cistack_run.apply, "wait_for_services", return_value=(True, "", "postgres")):
            with patch.object(cistack_run.stores, "converge", return_value=type("Report", (), {"ok": True})()):
                self.assertTrue(cistack_run._stores_stage(ci_stack(), host, sleep=lambda _: None).ok)
            self.assertTrue(cistack_run._start_stage(ci_stack(), host).ok)
        for command in host.commands:
            if command[:2] == ("docker", "compose"):
                self.assertEqual(command[2], "--project-directory")
                self.assertEqual(command[3], CI_ROOT)
                self.assertEqual(command[5], f"{CI_ROOT}/compose.yaml")

        client = type("Frontend", (), {"ready": lambda self: True})()
        with patch.object(cistack_run.apply, "wait_for_services", return_value=(True, "", "")):
            result = cistack_run._verify_stage(
                ci_stack(), host, client_factory=lambda **_: cast(owui.Client, client), sleep=lambda _: None
            )
        self.assertTrue(result.ok)
        self.assertIn(
            tuple(
                host_stack.exec_argv(
                    CI_ROOT,
                    API_SERVICE_NAME,
                    "python3",
                    "-c",
                    cistack_run._HEALTH_SCRIPT,
                )
            ),
            host.commands,
        )
        self.assertNotIn("/etc/gideon/rendered", " ".join(" ".join(command) for command in host.commands))
        self.assertNotIn("/data/backup-staging", " ".join(" ".join(command) for command in host.commands))


class DownAndSecrets(unittest.TestCase):
    def test_down_keeps_data_without_wipe_and_wipes_only_named_paths(self) -> None:
        host = FakeHost()
        store_paths = (
            CI_QDRANT_DATA_ROOT,
            CI_OPENSEARCH_DATA_ROOT,
            CI_OPENSEARCH_RENDERED_DIR,
        )
        self.assertTrue(set(store_paths) <= set(CI_WIPE_PATHS))
        host.directories.update(store_paths)
        settings_path = f"{CI_OPENSEARCH_RENDERED_DIR}/opensearch.yml"
        host.files[settings_path] = "fixture settings\n"
        compose_path = f"{CI_ROOT}/compose.yaml"
        host.files[compose_path] = "name: gideon-ci\n"
        generated = f"{CI_SECRETS_DIR}/postgres_superuser_password"
        admin_key = f"{CI_SECRETS_DIR}/gideon_admin_api_key"
        eval_key = f"{CI_SECRETS_DIR}/gideon_eval_api_key"
        for path in (generated, admin_key, eval_key):
            host.files[path] = "value\n"
        instruction_path = f"{CI_ROOT}/{API_INSTRUCTION_PATH}"
        host.files[instruction_path] = "fixture instruction\n"
        host.directories.add(f"{CI_ROOT}/{API_SERVICE_NAME}")

        self.assertEqual(cistack_run.down(ci_stack(), host), 0)
        self.assertIn(generated, host.files)
        self.assertTrue(set(store_paths) <= host.directories)
        self.assertTrue(any(command[:2] == ("docker", "compose") for command in host.commands))
        host.commands.clear()
        self.assertEqual(cistack_run.down(ci_stack(), host, wipe=True), 0)
        self.assertIn(generated, host.files)
        self.assertNotIn(admin_key, host.files)
        self.assertNotIn(eval_key, host.files)
        self.assertNotIn(instruction_path, host.files)
        self.assertNotIn(f"{CI_ROOT}/{API_SERVICE_NAME}", host.directories)
        self.assertFalse(set(store_paths) & host.directories)
        self.assertNotIn(settings_path, host.files)
        wipe_commands = [command for command in host.commands if command[0] == "rm"]
        self.assertEqual(wipe_commands, [("rm", "-rf", path) for path in CI_WIPE_PATHS])

    def test_down_holds_the_engine_lock_from_inspection_through_the_wipe(self) -> None:
        lock_path = cistack_run.backuplock.ENGINE_LOCK.path
        for wipe in (False, True):
            with self.subTest(wipe=wipe):
                host = FakeHost()
                host.files[f"{CI_ROOT}/compose.yaml"] = "name: gideon-ci\n"
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cistack_run.down(ci_stack(), host, wipe=wipe)

                self.assertEqual(code, 0)
                self.assertIn(
                    "preconditions: ok — CI project is present; engine lock taken",
                    output.getvalue(),
                )
                held = host.engine_lock_reads
                self.assertEqual(held[0], ("exists", True))
                self.assertIn(("docker", True), held)
                self.assertEqual(("rm", True) in held, wipe)
                self.assertTrue(all(locked for _, locked in held))
                self.assertEqual(host.lock_events, [("take", lock_path), ("release", lock_path)])
                self.assertNotIn(lock_path, host.locks)

    def test_down_refuses_a_held_engine_lock_before_any_argv(self) -> None:
        lock_path = cistack_run.backuplock.ENGINE_LOCK.path
        for wipe in (False, True):
            with self.subTest(wipe=wipe):
                host = FakeHost()
                compose_path = f"{CI_ROOT}/compose.yaml"
                host.files[compose_path] = "name: gideon-ci\n"
                host.directories.update(CI_WIPE_PATHS)
                holder = json.dumps({
                    "command": "tools.cistack smoke",
                    "pid": os.getpid() + 1,
                    "started": "2026-01-01T00:00:00+00:00",
                })
                host.locks[lock_path] = holder
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cistack_run.down(ci_stack(), host, wipe=wipe)

                self.assertEqual(code, 1)
                self.assertIn("preconditions: refuse", output.getvalue())
                self.assertIn("tools.cistack smoke", output.getvalue())
                self.assertIn(f"pid {os.getpid() + 1}", output.getvalue())
                self.assertIn(f"ps -p {os.getpid() + 1}", output.getvalue())
                self.assertEqual(host.commands, [])
                self.assertIn(compose_path, host.files)
                self.assertTrue(set(CI_WIPE_PATHS) <= host.directories)
                self.assertEqual(host.locks[lock_path], holder)

    def test_down_passes_a_nested_holder_and_leaves_its_lock(self) -> None:
        host = FakeHost()
        lock_path = cistack_run.backuplock.ENGINE_LOCK.path
        holder = json.dumps({
            "command": "tools.cistack smoke",
            "pid": os.getpid(),
            "started": "2026-01-01T00:00:00+00:00",
        })
        host.locks[lock_path] = holder
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cistack_run.down(ci_stack(), host, wipe=True)

        self.assertEqual(code, 0)
        self.assertIn("engine lock held by this process", output.getvalue())
        self.assertEqual(host.locks[lock_path], holder)
        self.assertEqual(host.lock_events, [("take", lock_path)])

    def test_failed_stop_or_wipe_still_releases_the_engine_lock(self) -> None:
        lock_path = cistack_run.backuplock.ENGINE_LOCK.path
        for failing in ("docker", "rm"):
            with self.subTest(failing=failing):
                host = FakeHost()
                host.files[f"{CI_ROOT}/compose.yaml"] = "name: gideon-ci\n"
                host.failing_programs.add(failing)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cistack_run.down(ci_stack(), host, wipe=True)

                self.assertEqual(code, 1)
                stage = "down" if failing == "docker" else "wipe"
                self.assertIn(f"{stage}: refuse", output.getvalue())
                self.assertEqual(host.lock_events, [("take", lock_path), ("release", lock_path)])
                self.assertNotIn(lock_path, host.locks)

    def test_non_root_down_refuses_before_the_engine_lock(self) -> None:
        host = FakeHost(euid=1000)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cistack_run.down(ci_stack(), host, wipe=True)

        self.assertEqual(code, 1)
        self.assertIn("root is required", output.getvalue())
        self.assertEqual(host.lock_events, [])
        self.assertEqual(host.commands, [])

    def test_sibling_mints_lexical_store_secrets_and_skips_unused_keys(self) -> None:
        self.assertEqual(
            set(CI_SKIPPED_SECRETS),
            {ENGINE_SECRET_NAME, EMBED_SECRET_NAME, "grafana_admin_password", SEARXNG_SECRET_NAME},
        )

    def test_selector_precedes_every_render_secret_read(self) -> None:
        host = FakeHost()
        events: list[str] = []
        fake_inputs = cast(RenderInputs, object())

        def select(path: Path) -> None:
            events.append(f"select:{path}")

        def ensure(_host: FakeHost, *, skip: Collection[str] = ()) -> EnsureResult:
            events.append(f"ensure:{','.join(skip)}")
            return EnsureResult()

        with (
            patch.object(cistack_run.secrets, "select_directory", select),
            patch.object(cistack_run.secrets, "ensure_generated", ensure),
            patch.object(cistack_run, "load_render_inputs", lambda *args, **kwargs: (fake_inputs, "", "", "")),
        ):
            result, inputs = cistack_run._secrets_stage(ci_stack(), host, site_path=SITE_PATH)
        self.assertTrue(result.ok)
        self.assertIs(inputs, fake_inputs)
        self.assertEqual(
            events, [f"select:{CI_SECRETS_DIR}", f"ensure:{','.join(CI_SKIPPED_SECRETS)}"]
        )

    def test_bootstrap_uses_the_in_memory_frontend_factory(self) -> None:
        host = FakeHost()
        host.files[f"{CI_ROOT}/open-webui/manifest.yaml"] = "groups: []\nmodels: []\nfunctions: []\n"
        frontend = object()
        calls: list[object] = []

        def factory(**kwargs: object) -> owui.Client:
            calls.append(kwargs)
            return cast(owui.Client, frontend)

        def bootstrap(
            _io: object,
            received_factory: object,
            manifest: object,
            *,
            rendered_dir: Path,
        ) -> owui.BootstrapReport:
            calls.extend((received_factory, manifest, rendered_dir))
            return owui.BootstrapReport()

        with (
            patch.object(cistack_run.owui, "wait_ready", return_value=owui.ReadyResult(True)),
            patch.object(cistack_run.owui, "bootstrap", bootstrap),
        ):
            result = cistack_run._manifest_stage(
                ci_stack(), host, client_factory=factory, sleep=lambda _: None
            )
        self.assertTrue(result.ok)
        self.assertEqual(calls[0], {})
        self.assertIs(calls[1], factory)
        self.assertEqual(calls[3], Path(CI_ROOT))

    def test_up_wipe_up_mints_both_database_issued_keys_again(self) -> None:
        host = FakeHost()
        manifest_path = f"{CI_ROOT}/open-webui/manifest.yaml"
        compose_path = f"{CI_ROOT}/compose.yaml"
        fake_inputs = cast(RenderInputs, object())
        key_names = ("gideon_admin_api_key", "gideon_eval_api_key")
        minted_values: list[tuple[str, str]] = []
        first_values: tuple[str, str] = ("", "")
        second_values: tuple[str, str] = ("", "")

        def render_stage(
            _stack: cistack_run.CiStack, _host: FakeHost, _inputs: RenderInputs
        ) -> StageResult:
            host.files[compose_path] = "name: gideon-ci\n"
            host.files[manifest_path] = "groups: []\nmodels: []\nfunctions: []\n"
            return StageResult("render", True, "", "")

        def bootstrap(
            _io: FakeHost,
            _factory: object,
            _manifest: object,
            *,
            rendered_dir: Path,
        ) -> owui.BootstrapReport:
            self.assertEqual(rendered_dir, Path(CI_ROOT))
            values: list[str] = []
            for name in key_names:
                value = f"{name}-{len(minted_values)}"
                self.assertIsNone(cistack_run.secrets.write_secret(host, name, value))
                values.append(value)
            minted_values.append((values[0], values[1]))
            return owui.BootstrapReport(minted=key_names)

        success = StageResult("stage", True, "", "")
        original_directory = cistack_run.secrets.current_directory()
        try:
            with (
                patch.object(
                    cistack_run,
                    "load_render_inputs",
                    return_value=(fake_inputs, "", "", ""),
                ),
                patch.object(cistack_run, "_preconditions", return_value=success),
                patch.object(cistack_run, "_render_stage", render_stage),
                patch.object(cistack_run, "_stores_stage", return_value=success),
                patch.object(cistack_run, "_start_stage", return_value=success),
                patch.object(cistack_run, "_verify_stage", return_value=success),
                patch.object(
                    cistack_run.owui,
                    "wait_ready",
                    return_value=owui.ReadyResult(True),
                ),
                patch.object(cistack_run.owui, "bootstrap", bootstrap),
            ):
                def client_factory(**_: object) -> owui.Client:
                    return cast(owui.Client, object())

                self.assertEqual(
                    cistack_run.up(
                        ci_stack(),
                        host,
                        site_path=SITE_PATH,
                        client_factory=client_factory,
                    ),
                    0,
                )
                first_values = tuple(
                    host.files[f"{CI_SECRETS_DIR}/{name}"].strip()
                    for name in key_names
                )  # type: ignore[assignment]
                self.assertEqual(cistack_run.down(ci_stack(), host, wipe=True), 0)
                self.assertEqual(
                    [host.files.get(f"{CI_SECRETS_DIR}/{name}") for name in key_names],
                    [None, None],
                )
                self.assertEqual(
                    cistack_run.up(
                        ci_stack(),
                        host,
                        site_path=SITE_PATH,
                        client_factory=client_factory,
                    ),
                    0,
                )
                second_values = tuple(
                    host.files[f"{CI_SECRETS_DIR}/{name}"].strip()
                    for name in key_names
                )  # type: ignore[assignment]
        finally:
            cistack_run.secrets.select_directory(original_directory)
        self.assertEqual(len(minted_values), 2)
        self.assertNotEqual(first_values, second_values)


class Status(unittest.TestCase):
    def test_relay_row_names_each_relay_on_its_production_network(self) -> None:
        """Status reads every relay and names both networks it keeps in use."""
        relays = (
            (cistack_run.RELAY_SERVICE_NAME, INTEGRATION_NETWORK_NAME),
            (cistack_run.RECORD_RELAY_SERVICE_NAME, INTERNAL_NETWORK_NAME),
            (cistack_run.EMBED_RELAY_SERVICE_NAME, INTERNAL_NETWORK_NAME),
        )
        for identity in ("absent", "name", "labels"):
            with self.subTest(identity=identity):
                host = FakeHost()
                host.files[f"{CI_ROOT}/compose.yaml"] = "services: {}\n"
                if identity != "absent":
                    for service, network in relays:
                        container = {
                            "Name": container_name(service, project=cistack_run.CI_PROJECT),
                        } if identity == "name" else {
                            "Name": f"fixture-{service}",
                            "Labels": {
                                "com.docker.compose.project": cistack_run.CI_PROJECT,
                                "com.docker.compose.service": service,
                            },
                        }
                        host.network_containers.setdefault(network_name(network), {})[service] = container
                output = io.StringIO()

                with contextlib.redirect_stdout(output):
                    code = cistack_run.status(ci_stack(), host)

                self.assertEqual(code, 0)
                state = "absent from" if identity == "absent" else "attached to"
                self.assertIn("relay: ok —", output.getvalue())
                for service, network in relays:
                    self.assertIn(
                        f"{service} is {state} the {network} network", output.getvalue(),
                    )
                if identity != "absent":
                    self.assertIn("integration and internal networks in use", output.getvalue())
                    self.assertIn("tools.cistack down before a restore or upgrade --rollback", output.getvalue())
                else:
                    self.assertNotIn("networks in use", output.getvalue())
                self.assertEqual(
                    {command for command in host.commands if command[:3] == ("docker", "network", "inspect")},
                    {("docker", "network", "inspect", network_name(network)) for _, network in relays},
                )


class Cli(unittest.TestCase):
    def test_default_client_is_plain_loopback_http(self) -> None:
        host = FakeHost()
        captured: dict[str, object] = {}

        def fake_up(
            ci: cistack_run.CiStack,
            _host: object,
            *,
            site_path: PathLike,
            client_factory: object,
        ) -> int:
            captured.update({"stack": ci, "site": site_path, "factory": client_factory})
            return 0

        with patch.object(cli.run, "up", fake_up):
            self.assertEqual(cli.main(["up"], host=host, checkout=ROOT, site_path=SITE_PATH), 0)
        stack_value = cast(cistack_run.CiStack, captured["stack"])
        self.assertEqual(stack_value.base_url, f"http://127.0.0.1:{CI_PORT}")
        self.assertEqual(captured["site"], SITE_PATH)

    def test_relative_checkout_is_made_absolute(self) -> None:
        captured: dict[str, object] = {}

        def fake_up(
            ci: cistack_run.CiStack,
            _host: object,
            *,
            site_path: PathLike,
            client_factory: object,
        ) -> int:
            captured["stack"] = ci
            return 0

        with patch.object(cli.run, "up", fake_up):
            self.assertEqual(
                cli.main(["up", "--checkout", "."], host=FakeHost(), site_path=SITE_PATH), 0
            )
        checkout = cast(cistack_run.CiStack, captured["stack"]).checkout
        self.assertTrue(checkout.is_absolute())
        self.assertEqual(checkout, Path.cwd().resolve())


class Imports(unittest.TestCase):
    def test_tool_imports_the_standard_library_yaml_gideon_and_tools_alone(self) -> None:
        allowed = set(sys.stdlib_module_names) | {"yaml", "gideon", "tools"}
        for path in sorted((Path(__file__).resolve().parents[1] / "tools" / "cistack").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    with self.subTest(path=path.name, module=name):
                        self.assertIn(name.split(".", 1)[0], allowed)
