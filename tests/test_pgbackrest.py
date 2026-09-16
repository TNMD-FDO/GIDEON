"""pgBackRest argv, repository, and JSON-info contracts."""

import os
import subprocess
import unittest
from collections.abc import Mapping

from gideon.host import pgbackrest, stack
from gideon.host.sysio import Command, PathLike

RENDERED = "/etc/gideon/rendered"
DRILL = "/data/drill"
COMPOSE = (
    "docker",
    "compose",
    "--project-directory",
    RENDERED,
    "-f",
    f"{RENDERED}/compose.yaml",
)


class FakeHost:
    """A dict-backed Host that records commands and seam mutations."""

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]],
    ) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[tuple[str, ...], float | None]] = []
        self.files: dict[str, str] = {}
        self.mkdir_calls: list[tuple[str, int, bool, bool]] = []
        self.chmod_calls: list[tuple[str, int]] = []
        self.chown_calls: list[tuple[str, int, int]] = []

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
        del check, input, cwd, env
        command = tuple(argv)
        self.calls.append((command, timeout))
        return self.responses.get(command, subprocess.CompletedProcess(list(command), 127, "", "missing fake response"))

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        return self.files[os.fspath(path)]

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
        del path
        return []

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(os.fspath(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        raise FileNotFoundError

    def chmod(self, path: PathLike, mode: int) -> None:
        self.chmod_calls.append((os.fspath(path), mode))

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.chown_calls.append((os.fspath(path), uid, gid))

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        self.mkdir_calls.append((os.fspath(path), mode, parents, exist_ok))

    def geteuid(self) -> int:
        return 0


def done(argv: tuple[str, ...], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), 0, stdout, "")


def failed(argv: tuple[str, ...], stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), 1, "", stderr)


class Argv(unittest.TestCase):
    def test_exec_runs_as_the_container_postgres_user(self) -> None:
        self.assertEqual(
            tuple(pgbackrest.exec_argv(RENDERED, "backup", "--type=full")),
            COMPOSE
            + (
                "exec",
                "-T",
                "-u",
                "postgres",
                "postgres",
                "pgbackrest",
                "--stanza=gideon",
                "backup",
                "--type=full",
            ),
        )

    def test_run_and_verify_mount_a_side_repository_only_when_given(self) -> None:
        self.assertEqual(
            tuple(pgbackrest.run_argv(DRILL, "restore")),
            (
                "docker",
                "compose",
                "--project-directory",
                DRILL,
                "-f",
                f"{DRILL}/compose.yaml",
                "run",
                "--rm",
                "--no-deps",
                "-u",
                "postgres",
                "postgres",
                "pgbackrest",
                "--stanza=gideon",
                "restore",
            ),
        )
        self.assertEqual(
            tuple(pgbackrest.verify_argv(DRILL, "/data/fetch")),
            (
                "docker",
                "compose",
                "--project-directory",
                DRILL,
                "-f",
                f"{DRILL}/compose.yaml",
                "run",
                "--rm",
                "--no-deps",
                "-u",
                "postgres",
                "-v",
                f"/data/fetch:{pgbackrest.SIDE_REPOSITORY_PATH}",
                "postgres",
                "pgbackrest",
                "--stanza=gideon",
                f"--repo1-path={pgbackrest.SIDE_REPOSITORY_PATH}",
                "verify",
            ),
        )

    def test_backup_builder_selects_type_and_expiry(self) -> None:
        self.assertEqual(
            tuple(pgbackrest.backup_argv(RENDERED, "incr", expire_auto=False)),
            COMPOSE
            + (
                "exec",
                "-T",
                "-u",
                "postgres",
                "postgres",
                "pgbackrest",
                "--stanza=gideon",
                "backup",
                "--type=incr",
                "--no-expire-auto",
            ),
        )
        with self.assertRaises(ValueError):
            pgbackrest.backup_argv(RENDERED, "differential")


class Operations(unittest.TestCase):
    def test_info_parses_full_and_incremental_and_finds_newest_full(self) -> None:
        argv = tuple(pgbackrest.exec_argv(RENDERED, "info", "--output=json"))
        document = (
            '[{"name":"gideon","repo":[{"backup":['
            '{"label":"20260901-010000F","type":"full","timestamp":{"stop":100},'
            '"archive":{"start":"000000020000000000000005","stop":"000000020000000000000007"}},'
            '{"label":"20260901-020000I","type":"incr","timestamp":{"stop":110}},'
            '{"label":"20260801-010000F","type":"full","timestamp":{"stop":90}}'
            ']}]}]'
        )
        host = FakeHost({argv: done(argv, document)})
        result = pgbackrest.info(host, RENDERED)
        self.assertTrue(result.ok)
        self.assertEqual(
            result.infos,
            (
                # The timeline is the archive start segment's first eight hex digits.
                pgbackrest.BackupInfo("20260901-010000F", "full", 100.0, timeline=2),
                pgbackrest.BackupInfo("20260901-020000I", "incr", 110.0),
                pgbackrest.BackupInfo("20260801-010000F", "full", 90.0),
            ),
        )
        self.assertEqual(
            pgbackrest.newest_full(result.infos),
            pgbackrest.BackupInfo("20260901-010000F", "full", 100.0, timeline=2),
        )

    def test_info_over_a_stopped_stack_uses_the_one_off_container(self) -> None:
        argv = tuple(pgbackrest.run_argv(RENDERED, "info", "--output=json"))
        document = '[{"name":"gideon","repo":[{"backup":[{"label":"20260901-010000F","type":"full","timestamp":{"stop":100},"archive":{"start":"000000030000000000000002"}}]}]}]'
        host = FakeHost({argv: done(argv, document)})
        result = pgbackrest.info(host, RENDERED, running=False)
        self.assertTrue(result.ok, result.problem)
        self.assertEqual(result.infos[0].timeline, 3)
        self.assertIn("run", argv)
        self.assertNotIn("exec", argv)

    def test_malformed_info_is_a_logs_bearing_problem(self) -> None:
        argv = tuple(pgbackrest.exec_argv(RENDERED, "info", "--output=json"))
        host = FakeHost({argv: done(argv, "not-json")})
        result = pgbackrest.info(host, RENDERED)
        self.assertFalse(result.ok)
        self.assertIn("malformed JSON", result.problem or "")
        self.assertEqual(result.fix, "docker compose -f /etc/gideon/rendered/compose.yaml logs postgres")

    def test_check_uses_a_bound_and_reports_the_logs_fix(self) -> None:
        argv = tuple(pgbackrest.exec_argv(RENDERED, "check"))
        host = FakeHost({argv: failed(argv, "archive check failed")})
        result = pgbackrest.check(host, RENDERED)
        self.assertFalse(result.ok)
        self.assertIn("archive check failed", result.problem or "")
        self.assertEqual(host.calls[0][1], 3600.0)
        self.assertIn("logs postgres", result.fix)

    def test_verify_returns_none_on_success_and_a_problem_on_failure(self) -> None:
        argv = tuple(pgbackrest.verify_argv(RENDERED))
        host = FakeHost({argv: done(argv)})
        self.assertIsNone(pgbackrest.verify(host, argv))
        host = FakeHost({argv: failed(argv, "checksum mismatch")})
        problem = pgbackrest.verify(host, argv)
        self.assertIn("checksum mismatch", problem or "")

    def test_stanza_mismatch_has_the_restore_fix_and_other_failures_have_logs(self) -> None:
        argv = tuple(pgbackrest.exec_argv(RENDERED, "stanza-create"))
        mismatch = FakeHost({argv: failed(argv, "info files do not match the database")})
        result = pgbackrest.ensure_stanza(mismatch, RENDERED)
        self.assertFalse(result.ok)
        self.assertIn("sudo python3 -m gideon restore --from staging", result.fix)
        self.assertIn("move /data/backup-staging/pgbackrest aside", result.fix)

        other = FakeHost({argv: failed(argv, "permission denied")})
        result = pgbackrest.ensure_stanza(other, RENDERED)
        self.assertFalse(result.ok)
        self.assertIn("logs postgres", result.fix)

    def test_existing_stanza_is_accepted(self) -> None:
        argv = tuple(pgbackrest.exec_argv(RENDERED, "stanza-create"))
        host = FakeHost({argv: failed(argv, "stanza already exists")})
        result = pgbackrest.ensure_stanza(host, RENDERED)
        self.assertTrue(result.ok)

    def test_repository_uses_resolved_container_ids_and_the_seam(self) -> None:
        uid = tuple(stack.exec_argv(RENDERED, "postgres", "id", "-u", "postgres"))
        gid = tuple(stack.exec_argv(RENDERED, "postgres", "id", "-g", "postgres"))
        host = FakeHost({uid: done(uid, "4321\n"), gid: done(gid, "5432\n")})
        result = pgbackrest.ensure_repository(host, RENDERED)
        self.assertTrue(result.ok)
        self.assertEqual(
            host.calls,
            [(uid, 60.0), (gid, 60.0)],
        )
        self.assertEqual(
            host.mkdir_calls,
            [(pgbackrest.REPOSITORY_PATH, 0o750, True, True)],
        )
        self.assertEqual(host.chmod_calls, [(pgbackrest.REPOSITORY_PATH, 0o750)])
        self.assertEqual(host.chown_calls, [(pgbackrest.REPOSITORY_PATH, 4321, 5432)])
