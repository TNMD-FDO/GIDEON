"""Store convergence: roles, databases, and the forward-only migration runner (spec §7.2, ADR-0005)."""

import os
import subprocess
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path

from gideon.host.stack import exec_argv
from gideon.host.stores import converge
from gideon.host.sysio import Command, PathLike

ROOT = Path(__file__).resolve().parent.parent
RENDERED = "/etc/gideon/rendered"
SECRETS = {
    "/etc/gideon/secrets/postgres_openwebui_password": "owui-pw\n",
    "/etc/gideon/secrets/postgres_gideon_password": "gid'eon-pw\n",
    "/etc/gideon/secrets/postgres_gideon_audit_password": "audit-pw\n",
    "/etc/gideon/secrets/postgres_gideon_ro_metrics_password": "metrics-pw\n",
}
MIGRATIONS = {
    f"{ROOT}/migrations/0001_audit_log.sql": (ROOT / "migrations/0001_audit_log.sql").read_text(),
    f"{ROOT}/migrations/0002_metrics_reader.sql": (ROOT / "migrations/0002_metrics_reader.sql").read_text(),
    f"{ROOT}/migrations/0003_guardrail_trips.sql": (ROOT / "migrations/0003_guardrail_trips.sql").read_text(),
    f"{ROOT}/migrations/0004_second.sql": "CREATE TABLE second (id int);\n",
    f"{ROOT}/migrations/README.md": "not a migration",
}

Responder = Callable[[tuple[str, ...], str | None], subprocess.CompletedProcess[str]]


def psql(role: str, database: str, *flags: str) -> tuple[str, ...]:
    return tuple(exec_argv(RENDERED, "postgres", "psql", "-U", role, "-d", database, *flags, "-f", "-"))


QUERY = ("-tA",)
STATEMENT = ("-v", "ON_ERROR_STOP=1")
MIGRATION = ("-v", "ON_ERROR_STOP=1", "--single-transaction")


class FakeHost:
    """Every run is answered by ``respond(argv, input)``; calls record argv and stdin."""

    def __init__(self, respond: Responder, files: Mapping[str, str]) -> None:
        self.respond = respond
        self.files = dict(files)
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run(self, argv: Command, *, check: bool = False, input: str | None = None, cwd: PathLike | None = None, env: Mapping[str, str] | None = None, timeout: float | None = None, passthrough: bool = False) -> subprocess.CompletedProcess[str]:
        del check, cwd, timeout
        assert env is None, "no environment may carry a credential"
        command = tuple(argv)
        self.calls.append((command, input))
        return self.respond(command, input)

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        raise NotImplementedError

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        prefix = os.fspath(path).rstrip("/") + "/"
        names = [key[len(prefix):] for key in self.files if key.startswith(prefix) and "/" not in key[len(prefix):]]
        if not names:
            raise FileNotFoundError(prefix)
        return names

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        raise NotImplementedError

    def stat(self, path: PathLike) -> os.stat_result:
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        raise NotImplementedError

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        raise NotImplementedError

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        raise NotImplementedError

    def geteuid(self) -> int:
        return 0


def ok(command: tuple[str, ...], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 0, stdout, "")


def fail(command: tuple[str, ...], stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 1, "", stderr)


class FreshServer:
    """Answers the queries of an empty server, remembering what converge created."""

    def __init__(self) -> None:
        self.roles: list[str] = ["postgres"]
        self.databases: list[str] = ["postgres", "template1"]
        self.versions: list[str] = []

    def __call__(self, command: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
        sql = stdin or ""
        if "FROM pg_roles" in sql:
            return ok(command, "\n".join(self.roles) + "\n")
        if "FROM pg_database" in sql:
            return ok(command, "\n".join(self.databases) + "\n")
        if "FROM schema_migrations" in sql:
            return ok(command, "\n".join(self.versions) + "\n")
        if "CREATE ROLE" in sql:
            self.roles.append(sql.split("CREATE ROLE ")[1].split()[0])
        elif "CREATE DATABASE" in sql:
            self.databases.append(sql.split("CREATE DATABASE ")[1].split()[0])
        elif "INSERT INTO schema_migrations" in sql:
            self.versions.append(sql.rsplit("('", 1)[1].split("'")[0])
        return ok(command)


class Converge(unittest.TestCase):
    def test_first_pass_creates_everything_in_order_with_sql_on_stdin_only(self) -> None:
        server = FreshServer()
        host = FakeHost(server, {**SECRETS, **MIGRATIONS})
        report = converge(host, RENDERED, root=ROOT)
        self.assertTrue(report.ok, report.problem)
        self.assertEqual(report.created_roles, ("openwebui", "gideon", "gideon_audit", "gideon_ro_metrics"))
        self.assertEqual(report.created_databases, ("openwebui", "gideon"))
        self.assertEqual(
            report.applied_migrations,
            ("0001_audit_log", "0002_metrics_reader", "0003_guardrail_trips", "0004_second"),
        )

        argvs = [call[0] for call in host.calls]
        self.assertEqual(argvs[0], psql("postgres", "postgres", *QUERY))
        role_statements = [call for call in host.calls if call[1] and "CREATE ROLE" in call[1]]
        self.assertEqual([call[0] for call in role_statements], [psql("postgres", "postgres", *STATEMENT)] * 4)
        gideon_sql = role_statements[1][1] or ""
        self.assertIn("\\set pw 'gid''eon-pw'\n", gideon_sql)
        self.assertIn("CREATE ROLE gideon LOGIN PASSWORD :'pw';", gideon_sql)
        for command, _ in host.calls:
            joined = " ".join(command)
            self.assertNotIn("owui-pw", joined)
            self.assertNotIn("eon-pw", joined)
            self.assertNotIn("audit-pw", joined)
            self.assertNotIn("metrics-pw", joined)
        database_statements = [call[1] for call in host.calls if call[1] and "CREATE DATABASE" in call[1]]
        self.assertEqual(database_statements, ["CREATE DATABASE openwebui OWNER openwebui;\n", "CREATE DATABASE gideon OWNER gideon;\n"])
        # Every migration-side call runs as gideon in gideon; the version row rides in the same transaction.
        self.assertIn(psql("gideon", "gideon", *STATEMENT), argvs)
        self.assertIn(psql("gideon", "gideon", *QUERY), argvs)
        migration_runs = [call for call in host.calls if call[0] == psql("gideon", "gideon", *MIGRATION)]
        self.assertEqual(len(migration_runs), 4)
        self.assertTrue((migration_runs[0][1] or "").startswith("-- §19.4"))
        self.assertTrue((migration_runs[0][1] or "").endswith("INSERT INTO schema_migrations (version) VALUES ('0001_audit_log');\n"))
        metrics_migration = next(call[1] or "" for call in migration_runs if "0002_metrics_reader" in (call[1] or ""))
        self.assertIn("GRANT USAGE ON SCHEMA public TO gideon_ro_metrics;", metrics_migration)
        self.assertIn("GRANT SELECT ON TABLE audit_log TO gideon_ro_metrics;", metrics_migration)
        # A migration runs as gideon, which may not grant a predefined role:
        # the pg_monitor membership is the superuser's, re-granted every run.
        self.assertNotIn("GRANT pg_monitor", metrics_migration)
        grants = [call for call in host.calls if call[1] and "GRANT pg_monitor" in call[1]]
        self.assertEqual(
            [(call[0], call[1]) for call in grants],
            [(psql("postgres", "postgres", *STATEMENT), "GRANT pg_monitor TO gideon_ro_metrics;\n")],
        )
        self.assertTrue(metrics_migration.endswith("INSERT INTO schema_migrations (version) VALUES ('0002_metrics_reader');\n"))
        self.assertLess(argvs.index(psql("gideon", "gideon", *STATEMENT)), argvs.index(psql("gideon", "gideon", *QUERY)))

    def test_second_pass_changes_nothing(self) -> None:
        server = FreshServer()
        host = FakeHost(server, {**SECRETS, **MIGRATIONS})
        converge(host, RENDERED, root=ROOT)
        host.calls.clear()
        report = converge(host, RENDERED, root=ROOT)
        self.assertTrue(report.ok)
        self.assertEqual((report.created_roles, report.created_databases, report.applied_migrations), ((), (), ()))
        mutations = [
            call[1]
            for call in host.calls
            if call[1] and any(text in call[1] for text in ("CREATE ROLE", "CREATE DATABASE", "INSERT INTO"))
        ]
        self.assertEqual(mutations, [])

    def test_existing_role_password_is_never_altered(self) -> None:
        server = FreshServer()
        server.roles.extend(("gideon", "gideon_ro_metrics"))
        host = FakeHost(server, {**SECRETS, **MIGRATIONS})
        report = converge(host, RENDERED, root=ROOT)
        self.assertTrue(report.ok, report.problem)
        self.assertEqual(report.created_roles, ("openwebui", "gideon_audit"))
        self.assertFalse(any(call[1] and "ALTER ROLE" in call[1] for call in host.calls))

    def test_missing_role_secret_refuses_before_any_statement(self) -> None:
        files = {**SECRETS, **MIGRATIONS}
        del files["/etc/gideon/secrets/postgres_gideon_password"]
        host = FakeHost(FreshServer(), files)
        report = converge(host, RENDERED, root=ROOT)
        self.assertFalse(report.ok)
        self.assertIn("postgres_gideon_password", report.problem or "")
        self.assertEqual(report.created_roles, ("openwebui",))

    def test_password_with_a_line_break_refuses(self) -> None:
        files = {**SECRETS, **MIGRATIONS, "/etc/gideon/secrets/postgres_gideon_password": "bad\rvalue\n"}
        report = converge(FakeHost(FreshServer(), files), RENDERED, root=ROOT)
        self.assertFalse(report.ok)
        self.assertIn("CR, LF, or NUL", report.problem or "")

    def test_role_statement_failure_reports_only_the_exit_status(self) -> None:
        def respond(command: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
            if stdin and "CREATE ROLE" in stdin:
                return fail(command, "ERROR: near \\set pw 'owui-pw'")
            return FreshServer()(command, stdin)

        report = converge(FakeHost(respond, {**SECRETS, **MIGRATIONS}), RENDERED, root=ROOT)
        self.assertFalse(report.ok)
        self.assertNotIn("owui-pw", report.problem or "")
        self.assertIn("exit 1", report.problem or "")
        self.assertIn("logs postgres", report.fix)

    def test_query_failure_names_the_logs_fix(self) -> None:
        def respond(command: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
            return fail(command, "could not connect")

        report = converge(FakeHost(respond, {**SECRETS, **MIGRATIONS}), RENDERED, root=ROOT)
        self.assertFalse(report.ok)
        self.assertIn("could not connect", report.problem or "")
        self.assertIn("docker compose", report.fix)

    def test_failed_migration_names_the_file_and_records_nothing_after_it(self) -> None:
        server = FreshServer()

        def respond(command: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
            if stdin and "CREATE TABLE second" in stdin:
                return fail(command, "ERROR: syntax error")
            return server(command, stdin)

        report = converge(FakeHost(respond, {**SECRETS, **MIGRATIONS}), RENDERED, root=ROOT)
        self.assertFalse(report.ok)
        self.assertIn("0004_second.sql", report.problem or "")
        self.assertIn("0004_second.sql", report.fix)
        self.assertEqual(report.applied_migrations, ("0001_audit_log", "0002_metrics_reader", "0003_guardrail_trips"))
        self.assertEqual(server.versions, ["0001_audit_log", "0002_metrics_reader", "0003_guardrail_trips"])

    def test_applied_versions_are_skipped_and_names_are_lexical(self) -> None:
        server = FreshServer()
        server.versions.append("0001_audit_log")
        host = FakeHost(server, {**SECRETS, **MIGRATIONS})
        report = converge(host, RENDERED, root=ROOT)
        self.assertEqual(report.applied_migrations, ("0002_metrics_reader", "0003_guardrail_trips", "0004_second"))

    def test_missing_migrations_directory_refuses(self) -> None:
        report = converge(FakeHost(FreshServer(), SECRETS), RENDERED, root=ROOT)
        self.assertFalse(report.ok)
        self.assertIn("migrations", report.problem or "")


class Migration0001(unittest.TestCase):
    def test_the_committed_migration_upholds_the_audit_contract(self) -> None:
        text = MIGRATIONS[f"{ROOT}/migrations/0001_audit_log.sql"]
        self.assertIn("PARTITION BY RANGE (at)", text)
        self.assertIn("SECURITY DEFINER", text)
        self.assertIn("SET search_path = pg_catalog, public", text)
        self.assertIn("CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.audit_log", text)
        self.assertIn("GRANT INSERT ON TABLE audit_log TO gideon_audit", text)
        self.assertNotIn("GRANT SELECT", text)
        self.assertNotIn("schema_migrations", text)


class Migration0002(unittest.TestCase):
    def test_metrics_reader_is_read_only_and_uses_the_runner_version_row(self) -> None:
        text = MIGRATIONS[f"{ROOT}/migrations/0002_metrics_reader.sql"]
        self.assertIn("GRANT USAGE ON SCHEMA public TO gideon_ro_metrics;", text)
        self.assertIn("GRANT SELECT ON TABLE audit_log TO gideon_ro_metrics;", text)
        self.assertNotIn("GRANT pg_monitor", text)
        self.assertNotIn("INSERT", text)
        self.assertNotIn("schema_migrations", text)


class Migration0003(unittest.TestCase):
    def test_guardrail_trips_is_append_only_and_partitioned(self) -> None:
        text = MIGRATIONS[f"{ROOT}/migrations/0003_guardrail_trips.sql"]
        self.assertIn("PARTITION BY RANGE (at)", text)
        self.assertIn("SECURITY DEFINER", text)
        self.assertIn("SET search_path = pg_catalog, public", text)
        self.assertIn("CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.guardrail_trips", text)
        self.assertIn("GRANT INSERT ON TABLE guardrail_trips TO gideon_audit", text)
        self.assertIn("GRANT SELECT ON TABLE guardrail_trips TO gideon_ro_metrics", text)
        self.assertIn("CHECK (source IN ('user', 'eval'))", text)
        self.assertNotIn("GRANT UPDATE", text)
        self.assertNotIn("GRANT DELETE", text)
        self.assertNotIn("schema_migrations", text)
