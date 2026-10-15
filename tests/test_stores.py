"""Store convergence: roles, databases, and the forward-only migration runner."""

import os
import re
import subprocess
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path

from gideon.host import models
from gideon.host.index import sample
from gideon.host.render import worker
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
    "/etc/gideon/secrets/postgres_gideon_eval_password": "eval-pw\n",
    "/etc/gideon/secrets/postgres_gideon_worker_password": "worker-pw\n",
}
MIGRATIONS = {
    f"{ROOT}/migrations/0001_audit_log.sql": (ROOT / "migrations/0001_audit_log.sql").read_text(),
    f"{ROOT}/migrations/0002_metrics_reader.sql": (ROOT / "migrations/0002_metrics_reader.sql").read_text(),
    f"{ROOT}/migrations/0003_guardrail_trips.sql": (ROOT / "migrations/0003_guardrail_trips.sql").read_text(),
    f"{ROOT}/migrations/0004_eval_runs.sql": (ROOT / "migrations/0004_eval_runs.sql").read_text(),
    f"{ROOT}/migrations/0005_eval_runs_decision.sql": (ROOT / "migrations/0005_eval_runs_decision.sql").read_text(),
    f"{ROOT}/migrations/0006_guardrail_trips_chat_id.sql": (ROOT / "migrations/0006_guardrail_trips_chat_id.sql").read_text(),
    f"{ROOT}/migrations/0007_procrastinate_queue.sql": (ROOT / "migrations/0007_procrastinate_queue.sql").read_text(),
    f"{ROOT}/migrations/0008_corpus_lockfiles.sql": (ROOT / "migrations/0008_corpus_lockfiles.sql").read_text(),
    f"{ROOT}/migrations/0009_upstream_observations.sql": (ROOT / "migrations/0009_upstream_observations.sql").read_text(),
    f"{ROOT}/migrations/0010_documents_opinions.sql": (ROOT / "migrations/0010_documents_opinions.sql").read_text(),
    f"{ROOT}/migrations/0011_second.sql": "CREATE TABLE second (id int);\n",
    f"{ROOT}/migrations/0015_search_turns.sql": (ROOT / "migrations/0015_search_turns.sql").read_text(),
    f"{ROOT}/migrations/0017_index_generations.sql": (ROOT / "migrations/0017_index_generations.sql").read_text(),
    f"{ROOT}/migrations/0018_chunk_embeddings.sql": (ROOT / "migrations/0018_chunk_embeddings.sql").read_text(),
    f"{ROOT}/migrations/0019_known_overruled.sql": (ROOT / "migrations/0019_known_overruled.sql").read_text(),
    f"{ROOT}/migrations/0021_dupe_clusters.sql": (ROOT / "migrations/0021_dupe_clusters.sql").read_text(),
    f"{ROOT}/migrations/0023_generation_documents.sql": (ROOT / "migrations/0023_generation_documents.sql").read_text(),
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
        self.assertEqual(report.created_roles, ("openwebui", "gideon", "gideon_audit", "gideon_ro_metrics", "gideon_eval", "gideon_worker"))
        self.assertEqual(report.created_databases, ("openwebui", "gideon"))
        self.assertEqual(
            report.applied_migrations,
            (
                "0001_audit_log",
                "0002_metrics_reader",
                "0003_guardrail_trips",
                "0004_eval_runs",
                "0005_eval_runs_decision",
                "0006_guardrail_trips_chat_id",
                "0007_procrastinate_queue",
                "0008_corpus_lockfiles",
                "0009_upstream_observations",
                "0010_documents_opinions",
                "0011_second",
                "0015_search_turns",
                "0017_index_generations",
                "0018_chunk_embeddings",
                "0019_known_overruled",
                "0021_dupe_clusters",
                "0023_generation_documents",
            ),
        )

        argvs = [call[0] for call in host.calls]
        self.assertEqual(argvs[0], psql("postgres", "postgres", *QUERY))
        role_statements = [call for call in host.calls if call[1] and "CREATE ROLE" in call[1]]
        self.assertEqual([call[0] for call in role_statements], [psql("postgres", "postgres", *STATEMENT)] * 6)
        gideon_sql = role_statements[1][1] or ""
        self.assertIn("\\set pw 'gid''eon-pw'\n", gideon_sql)
        self.assertIn("CREATE ROLE gideon LOGIN PASSWORD :'pw';", gideon_sql)
        for command, _ in host.calls:
            joined = " ".join(command)
            self.assertNotIn("owui-pw", joined)
            self.assertNotIn("eon-pw", joined)
            self.assertNotIn("audit-pw", joined)
            self.assertNotIn("metrics-pw", joined)
            self.assertNotIn("eval-pw", joined)
            self.assertNotIn("worker-pw", joined)
        database_statements = [call[1] for call in host.calls if call[1] and "CREATE DATABASE" in call[1]]
        self.assertEqual(database_statements, ["CREATE DATABASE openwebui OWNER openwebui;\n", "CREATE DATABASE gideon OWNER gideon;\n"])
        # Every migration-side call runs as gideon in gideon; the version row rides in the same transaction.
        self.assertIn(psql("gideon", "gideon", *STATEMENT), argvs)
        self.assertIn(psql("gideon", "gideon", *QUERY), argvs)
        migration_runs = [call for call in host.calls if call[0] == psql("gideon", "gideon", *MIGRATION)]
        self.assertEqual(len(migration_runs), sum(path.endswith(".sql") for path in MIGRATIONS))
        first_migration = MIGRATIONS[f"{ROOT}/migrations/0001_audit_log.sql"]
        self.assertTrue((migration_runs[0][1] or "").startswith(first_migration))
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
        server.roles.extend(("gideon", "gideon_ro_metrics", "gideon_eval", "gideon_worker"))
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
        self.assertIn("0011_second.sql", report.problem or "")
        self.assertIn("0011_second.sql", report.fix)
        self.assertEqual(
            report.applied_migrations,
            (
                "0001_audit_log",
                "0002_metrics_reader",
                "0003_guardrail_trips",
                "0004_eval_runs",
                "0005_eval_runs_decision",
                "0006_guardrail_trips_chat_id",
                "0007_procrastinate_queue",
                "0008_corpus_lockfiles",
                "0009_upstream_observations",
                "0010_documents_opinions",
            ),
        )
        self.assertEqual(
            server.versions,
            [
                "0001_audit_log",
                "0002_metrics_reader",
                "0003_guardrail_trips",
                "0004_eval_runs",
                "0005_eval_runs_decision",
                "0006_guardrail_trips_chat_id",
                "0007_procrastinate_queue",
                "0008_corpus_lockfiles",
                "0009_upstream_observations",
                "0010_documents_opinions",
            ],
        )

    def test_applied_versions_are_skipped_and_names_are_lexical(self) -> None:
        server = FreshServer()
        server.versions.append("0001_audit_log")
        host = FakeHost(server, {**SECRETS, **MIGRATIONS})
        report = converge(host, RENDERED, root=ROOT)
        self.assertEqual(
            report.applied_migrations,
            (
                "0002_metrics_reader",
                "0003_guardrail_trips",
                "0004_eval_runs",
                "0005_eval_runs_decision",
                "0006_guardrail_trips_chat_id",
                "0007_procrastinate_queue",
                "0008_corpus_lockfiles",
                "0009_upstream_observations",
                "0010_documents_opinions",
                "0011_second",
                "0015_search_turns",
                "0017_index_generations",
                "0018_chunk_embeddings",
                "0019_known_overruled",
                "0021_dupe_clusters",
                "0023_generation_documents",
            ),
        )

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


class Migration0004(unittest.TestCase):
    def test_eval_tables_are_partitioned_and_append_only(self) -> None:
        text = MIGRATIONS[f"{ROOT}/migrations/0004_eval_runs.sql"]
        self.assertIn("CREATE TABLE eval_runs", text)
        self.assertIn("PARTITION BY RANGE (started_at)", text)
        self.assertIn("PRIMARY KEY (run_id, started_at)", text)
        self.assertIn("CREATE TABLE eval_results", text)
        self.assertIn("PARTITION BY RANGE (run_started_at)", text)
        self.assertIn("PRIMARY KEY (run_id, case_id, repeat, run_started_at)", text)
        self.assertIn("CHECK (stack IN ('production', 'ci'))", text)
        self.assertIn("'smoke', 'nightly', 'weekly-off', 'decision'", text)
        self.assertIn("'candidate', 'engine-verify', 'manual'", text)
        self.assertEqual(text.count("CHECK (verdict IN ('pass', 'fail'))"), 2)
        self.assertIn("repeats integer NOT NULL CHECK (repeats >= 1)", text)
        self.assertIn("repeat integer NOT NULL CHECK (repeat >= 1)", text)
        self.assertIn("overrides jsonb NOT NULL DEFAULT '{}'::jsonb", text)

    def test_partition_functions_are_hardened_and_grants_are_minimal(self) -> None:
        text = MIGRATIONS[f"{ROOT}/migrations/0004_eval_runs.sql"]
        self.assertEqual(text.count("SECURITY DEFINER"), 2)
        self.assertEqual(text.count("SET search_path = pg_catalog, public"), 2)
        self.assertIn("public.%I PARTITION OF public.eval_runs", text)
        self.assertIn("public.%I PARTITION OF public.eval_results", text)
        self.assertEqual(text.count("to_char(month_start, 'YYYYMM')"), 2)
        self.assertIn("ALTER FUNCTION eval_runs_ensure_partition(timestamptz) OWNER TO gideon;", text)
        self.assertIn("ALTER FUNCTION eval_results_ensure_partition(timestamptz) OWNER TO gideon;", text)
        self.assertEqual(text.count("REVOKE ALL ON FUNCTION"), 2)
        self.assertEqual(text.count("GRANT EXECUTE ON FUNCTION"), 2)
        self.assertIn("GRANT USAGE ON SCHEMA public TO gideon_eval;", text)
        self.assertIn("GRANT INSERT ON TABLE eval_runs TO gideon_eval;", text)
        self.assertIn("GRANT INSERT ON TABLE eval_results TO gideon_eval;", text)
        self.assertIn("GRANT SELECT ON TABLE eval_runs TO gideon_ro_metrics;", text)
        self.assertIn("GRANT SELECT ON TABLE eval_results TO gideon_ro_metrics;", text)
        self.assertNotIn("GRANT UPDATE", text)
        self.assertNotIn("GRANT DELETE", text)
        self.assertNotIn("GRANT SELECT ON TABLE eval_runs TO gideon_eval", text)
        self.assertNotIn("GRANT SELECT ON TABLE eval_results TO gideon_eval", text)
        self.assertNotIn("REFERENCES", text)
        self.assertNotIn("schema_migrations", text)


class Migration0005(unittest.TestCase):
    def test_decision_columns_keep_existing_runs_and_allow_no_decision(self) -> None:
        text = MIGRATIONS[f"{ROOT}/migrations/0005_eval_runs_decision.sql"]
        self.assertIn("ALTER TABLE eval_runs", text)
        self.assertIn("ADD COLUMN forced boolean NOT NULL DEFAULT false", text)
        self.assertIn("ADD COLUMN partial boolean NOT NULL DEFAULT false", text)
        self.assertIn("ADD COLUMN decision jsonb", text)


class Migration0007(unittest.TestCase):
    def test_worker_grants_are_exact_and_keep_finished_jobs(self) -> None:
        text = MIGRATIONS[f"{ROOT}/migrations/0007_procrastinate_queue.sql"]
        expected = (
            "GRANT USAGE ON SCHEMA public TO gideon_worker;",
            "GRANT SELECT, INSERT, UPDATE ON TABLE procrastinate_jobs TO gideon_worker;",
            "GRANT INSERT ON TABLE procrastinate_events TO gideon_worker;",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE procrastinate_workers TO gideon_worker;",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE procrastinate_periodic_defers TO gideon_worker;",
            "GRANT USAGE ON SEQUENCE procrastinate_jobs_id_seq TO gideon_worker;",
            "GRANT USAGE ON SEQUENCE procrastinate_events_id_seq TO gideon_worker;",
            "GRANT USAGE ON SEQUENCE procrastinate_periodic_defers_id_seq TO gideon_worker;",
        )
        self.assertEqual(
            tuple(line for line in text.splitlines() if line.startswith("GRANT ")),
            expected,
            "Fix: restore the worker's reviewed table and sequence grants.",
        )
        jobs_grant = re.search(r"^GRANT .+ ON TABLE procrastinate_jobs TO gideon_worker;$", text, re.M)
        self.assertIsNotNone(jobs_grant, "Fix: restore the worker's jobs-table grant.")
        assert jobs_grant is not None
        self.assertNotIn(
            "DELETE", jobs_grant.group(),
            "Fix: keep finished jobs; remove DELETE from the jobs-table grant.",
        )
        self.assertNotIn(
            "SECURITY DEFINER", text,
            "Fix: leave queue functions as invoker functions.",
        )
        self.assertNotIn(
            "schema_migrations", text,
            "Fix: leave version bookkeeping to the migration runner.",
        )


class Migration0009(unittest.TestCase):
    def test_observations_are_append_only_and_metrics_can_read_bindings(self) -> None:
        text = MIGRATIONS[f"{ROOT}/migrations/0009_upstream_observations.sql"]
        self.assertIn("PRIMARY KEY (source, observed_at)", text)
        self.assertIn("CHECK (outcome IN ('observed', 'unanswered'))", text)
        self.assertIn("(outcome = 'observed') = (latest_label IS NOT NULL)", text)
        self.assertIn("(outcome = 'unanswered') = (detail IS NOT NULL)", text)
        grants = tuple(line for line in text.splitlines() if line.startswith("GRANT "))
        self.assertEqual(grants, (
            "GRANT SELECT, INSERT ON TABLE upstream_observations TO gideon_worker;",
            "GRANT SELECT ON TABLE upstream_observations TO gideon_ro_metrics;",
            "GRANT SELECT ON TABLE corpus_lockfiles TO gideon_ro_metrics;",
            "GRANT SELECT ON TABLE source_snapshots TO gideon_ro_metrics;",
            "GRANT SELECT ON TABLE lockfile_sources TO gideon_ro_metrics;",
        ))
        self.assertNotIn("schema_migrations", text)


class Migration0017(unittest.TestCase):
    """Generation identity, lifecycle checks, and record access are constrained."""

    def setUp(self) -> None:
        self.text = MIGRATIONS[f"{ROOT}/migrations/0017_index_generations.sql"]

    def test_identity_and_input_checks_match_the_host_grammar(self) -> None:
        self.assertIn("CREATE TABLE index_generations", self.text)
        self.assertIn("gen_id text PRIMARY KEY", self.text)
        base_pattern = worker.GENERATION_PATTERN.removesuffix(
            rf"(?:__{worker.SAMPLE_SUFFIX})?"
        )
        self.assertIn(f"gen_id ~ '^{base_pattern}$'", self.text)
        manifest = MIGRATIONS[f"{ROOT}/migrations/0023_generation_documents.sql"]
        self.assertIn("ALTER TABLE index_generations DROP CONSTRAINT index_generations_gen_id_check;", manifest)
        self.assertIn("ALTER TABLE index_generations ADD CONSTRAINT index_generations_gen_id_check", manifest)
        self.assertIn(f"gen_id ~ '^{worker.GENERATION_PATTERN}$'", manifest)
        self.assertIn("octet_length(gen_id) < 255", manifest)
        self.assertIn("octet_length(gen_id) < 255", self.text)
        self.assertIn(
            f"embedding_space ~ '{models.EMBEDDING_SPACE_ID.pattern}'", self.text,
        )
        self.assertIn("corpus_lockfile text NOT NULL REFERENCES corpus_lockfiles(label)", self.text)
        self.assertIn("lockfiles text[] NOT NULL", self.text)
        self.assertIn("cardinality(lockfiles) > 0 AND lockfiles @> ARRAY[corpus_lockfile]", self.text)
        self.assertIn("pipeline text NOT NULL CHECK (pipeline ~ '^[0-9]+\\.[0-9]+\\.[0-9]+$')", self.text)
        self.assertIn("inputs_hash text NOT NULL CHECK (inputs_hash ~ '^[0-9a-f]{64}$')", self.text)
        self.assertIn("UNIQUE (collection, inputs_hash)", self.text)
        for column, expected in (
            ("collection", set(worker.COLLECTION_NAMES)),
            ("state", set(worker.GENERATION_STATES)),
            ("failure_reason", worker.LEXICAL_FAILURE_REASONS),
        ):
            with self.subTest(column=column):
                match = re.search(rf"CHECK \({column} IN \((.*?)\)\)", self.text, re.S)
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(set(re.findall(r"'([^']+)'", match.group(1))), expected)

    def test_lifecycle_checks_and_progress_defaults(self) -> None:
        sql = " ".join(self.text.split())
        for check in (
            "CHECK ((state = 'failed') = (failure_reason IS NOT NULL))",
            "CHECK ((built_at IS NOT NULL) = ( lexical_built_at IS NOT NULL AND dense_built_at IS NOT NULL ))",
            "CHECK (promoted_at IS NULL OR state IN ('live', 'retired'))",
            "CHECK ((state = 'retired') = (retired_at IS NOT NULL))",
        ):
            with self.subTest(check=check):
                self.assertIn(check, sql)
        self.assertIn("state text NOT NULL", sql)
        self.assertIn("counts jsonb NOT NULL DEFAULT '{}'::jsonb", sql)
        self.assertIn("bytes bigint NOT NULL DEFAULT 0 CHECK (bytes >= 0)", sql)
        self.assertIn("created_at timestamptz NOT NULL DEFAULT now()", sql)
        for column in ("lexical_built_at", "dense_built_at", "built_at", "promoted_at", "retired_at"):
            self.assertIn(f"{column} timestamptz", sql)

    def test_grants_allow_progress_and_metrics_reads_without_deletion(self) -> None:
        grants = tuple(line for line in self.text.splitlines() if line.startswith("GRANT "))
        self.assertEqual(grants, (
            "GRANT SELECT, INSERT, UPDATE ON TABLE index_generations TO gideon_worker;",
            "GRANT SELECT ON TABLE index_generations TO gideon_ro_metrics;",
        ))
        self.assertNotIn("DELETE", self.text)
        self.assertNotIn("schema_migrations", self.text)
        manifest = MIGRATIONS[f"{ROOT}/migrations/0023_generation_documents.sql"]
        self.assertEqual(tuple(line for line in manifest.splitlines() if line.startswith("GRANT ")), (
            "GRANT SELECT, INSERT ON TABLE generation_documents TO gideon_worker;",
            "GRANT SELECT ON TABLE generation_documents TO gideon_ro_metrics;",
        ))
        self.assertNotIn("UPDATE", manifest)
        self.assertNotIn("DELETE", manifest)


class Migration0020(unittest.TestCase):
    """Manifest rows retain document identity and allow append-only worker writes."""

    def setUp(self) -> None:
        self.text = MIGRATIONS[f"{ROOT}/migrations/0023_generation_documents.sql"]
        self.sql = " ".join(self.text.split())

    def test_manifest_has_its_generation_key_hashes_and_write_timestamp(self) -> None:
        self.assertIn("CREATE TABLE generation_documents", self.sql)
        self.assertIn("gen_id text NOT NULL REFERENCES index_generations(gen_id)", self.sql)
        self.assertIn("PRIMARY KEY (gen_id, source_id)", self.sql)
        self.assertIn(f"source_id ~ '^{sample.SOURCE_ID_PATTERN}$'", self.sql)
        self.assertIn(f"char_length(source_id) BETWEEN 1 AND {sample.SOURCE_ID_MAX_LENGTH}", self.sql)
        for column in ("canonical_text_sha256", "doc_id"):
            self.assertIn(f"{column} text NOT NULL CHECK ({column} ~ '^[0-9a-f]{{64}}$')", self.sql)
        self.assertIn("indexed_at timestamptz NOT NULL DEFAULT now()", self.sql)
        self.assertNotIn("schema_migrations", self.text)

    def test_manifest_grants_only_worker_append_and_metrics_read(self) -> None:
        grants = tuple(line for line in self.text.splitlines() if line.startswith("GRANT "))
        self.assertEqual(grants, (
            f"GRANT SELECT, INSERT ON TABLE generation_documents TO {worker.WORKER_ROLE};",
            f"GRANT SELECT ON TABLE generation_documents TO {worker.METRICS_ROLE};",
        ))
        self.assertNotIn("UPDATE", self.text)
        self.assertNotIn("DELETE", self.text)

    def test_replacement_generation_check_keeps_the_byte_limit_and_optional_sample(self) -> None:
        self.assertIn("ALTER TABLE index_generations DROP CONSTRAINT index_generations_gen_id_check;", self.sql)
        self.assertIn("ALTER TABLE index_generations ADD CONSTRAINT index_generations_gen_id_check", self.sql)
        match = re.search(r"gen_id ~ '([^']+)'", self.text)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), f"^{worker.GENERATION_PATTERN}$")
        self.assertIn("octet_length(gen_id) < 255", self.sql)
        base = f"{worker.COLLECTION_NAMES[0]}__corpus-2099-01-03__fictitious-space"
        for value in (base, sample.sample_name(base)):
            with self.subTest(value=value):
                self.assertIsNotNone(re.fullmatch(match.group(1), value))
        for suffix in ("__unknown", "__sample__sample", "__sample-extra"):
            with self.subTest(suffix=suffix):
                self.assertIsNone(re.fullmatch(match.group(1), base + suffix))


class Migration0019(unittest.TestCase):
    """Listed pairs retain their snapshot identity and unresolved decisions."""

    def setUp(self) -> None:
        self.text = MIGRATIONS[f"{ROOT}/migrations/0019_known_overruled.sql"]

    def test_pair_identity_is_bound_to_both_source_snapshots(self) -> None:
        self.assertIn("CREATE TABLE known_overruled", self.text)
        self.assertIn("row_id text PRIMARY KEY CHECK (row_id ~ '^[0-9a-f]{64}$')", self.text)
        for source, snapshot in (
            ("source", "source_snapshot"),
            ("caselaw_source", "caselaw_snapshot"),
        ):
            with self.subTest(source=source):
                self.assertIn(f"{source} text NOT NULL", self.text)
                self.assertIn(f"{snapshot} date NOT NULL", self.text)
                self.assertIn(
                    f"FOREIGN KEY ({source}, {snapshot}) REFERENCES source_snapshots(source, snapshot_date)",
                    self.text,
                )
        self.assertIn("list_order integer NOT NULL CHECK (list_order > 0)", self.text)
        self.assertIn("ordinal integer NOT NULL CHECK (ordinal >= 0)", self.text)
        self.assertIn(
            "UNIQUE (source, source_snapshot, caselaw_source, caselaw_snapshot, list_order, ordinal)",
            self.text,
        )

    def test_resolution_and_qualifier_checks_match_the_vocabularies(self) -> None:
        for column, expected in (
            ("overruling_resolution", {"cite", "docket", "ambiguous", "absent"}),
            ("overruled_resolution", {"cite", "name", "ambiguous", "absent"}),
            ("qualifier", {"in_part", "on_other_grounds", "none"}),
        ):
            with self.subTest(column=column):
                self.assertIn(f"{column} text NOT NULL", self.text)
                match = re.search(rf"CHECK \({column} IN \((.*?)\)\)", self.text, re.S)
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(set(re.findall(r"'([^']+)'", match.group(1))), expected)
        for decision in ("overruling", "overruled"):
            with self.subTest(decision=decision):
                self.assertIn(f"{decision}_name text NOT NULL", self.text)
                self.assertIn(f"{decision}_cite text,", self.text)
                self.assertIn(f"{decision}_cluster bigint,", self.text)
                self.assertIn(
                    f"CHECK (({decision}_cluster IS NULL) = ({decision}_resolution IN ('ambiguous', 'absent')))",
                    self.text,
                )
        self.assertIn("overruling_docket text,", self.text)
        self.assertIn("overruling_year integer NOT NULL", self.text)
        self.assertIn("overruled_year integer,", self.text)
        self.assertIn("found_at timestamptz NOT NULL", self.text)

    def test_cluster_index_and_grants_preserve_append_only_rows(self) -> None:
        sql = " ".join(self.text.split())
        self.assertIn(
            "CREATE INDEX known_overruled_overruled_cluster_idx ON known_overruled (overruled_cluster) "
            "WHERE overruled_cluster IS NOT NULL;",
            sql,
        )
        grants = tuple(line for line in self.text.splitlines() if line.startswith("GRANT "))
        self.assertEqual(grants, (
            "GRANT SELECT, INSERT ON TABLE known_overruled TO gideon_worker;",
            "GRANT SELECT ON TABLE known_overruled TO gideon_ro_metrics;",
        ))
        self.assertNotIn("UPDATE", self.text)
        self.assertNotIn("DELETE", self.text)
        self.assertNotIn("schema_migrations", self.text)


class Migration0021(unittest.TestCase):
    """Clustering has generation-scoped keys, constrained counts, and limited grants."""

    def setUp(self) -> None:
        self.text = MIGRATIONS[f"{ROOT}/migrations/0021_dupe_clusters.sql"]

    def test_clusters_and_members_are_constrained_and_cascade_with_the_generation(self) -> None:
        sql = " ".join(self.text.split())
        for clause in (
            "CREATE TABLE dupe_clusters (",
            "gen_id text NOT NULL REFERENCES index_generations(gen_id) ON DELETE CASCADE",
            "PRIMARY KEY (gen_id, cluster_id)",
            "chunk_count bigint NOT NULL CHECK (chunk_count > 0)",
            "opinion_count bigint NOT NULL CHECK (opinion_count > 0 AND opinion_count <= chunk_count)",
            "boilerplate boolean NOT NULL",
            "CREATE TABLE dupe_members ( gen_id text NOT NULL",
            "PRIMARY KEY (gen_id, content_hash)",
            "FOREIGN KEY (gen_id, cluster_id) REFERENCES dupe_clusters(gen_id, cluster_id) ON DELETE CASCADE",
            "CHECK (cluster_id <> content_hash)",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, sql)
        self.assertEqual(sql.count("cluster_id text NOT NULL CHECK (cluster_id ~ '^[0-9a-f]{64}$')"), 2)
        self.assertIn("content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$')", sql)
        self.assertIn("ALTER TABLE index_generations ADD COLUMN clustered_at timestamptz;", sql)
        self.assertNotIn("ALTER TABLE chunks", sql)

    def test_grants_allow_whole_set_rebuilds_and_metrics_reads(self) -> None:
        grants = {line for line in self.text.splitlines() if line.startswith("GRANT ")}
        self.assertEqual(grants, {
            f"GRANT {privileges} ON TABLE {table} TO {role};"
            for table in ("dupe_clusters", "dupe_members")
            for privileges, role in (
                ("SELECT, INSERT, DELETE", worker.WORKER_ROLE),
                ("SELECT", "gideon_ro_metrics"),
            )
        })
        self.assertNotIn("schema_migrations", self.text)

    def test_failure_reason_check_is_replaced_with_the_shared_union(self) -> None:
        drop = "ALTER TABLE index_generations DROP CONSTRAINT index_generations_failure_reason_check;"
        add = "ALTER TABLE index_generations ADD CONSTRAINT index_generations_failure_reason_check"
        self.assertIn(drop, self.text)
        self.assertIn(add, self.text)
        self.assertLess(self.text.index(drop), self.text.index(add))
        match = re.search(r"CHECK \(failure_reason IN \((.*?)\)\)", self.text, re.S)
        assert match is not None
        reasons = set(re.findall(r"'([^']+)'", match.group(1)))
        self.assertEqual(reasons, worker.GENERATION_FAILURE_REASONS)
        previous = MIGRATIONS[f"{ROOT}/migrations/0018_chunk_embeddings.sql"]
        previous_match = re.search(r"CHECK \(failure_reason IN \((.*?)\)\)", previous, re.S)
        assert previous_match is not None
        self.assertEqual(reasons, set(re.findall(r"'([^']+)'", previous_match.group(1))) | {"cluster-budget"})

    def test_build_report_checks_are_widened_without_changing_the_coupling(self) -> None:
        previous = (ROOT / "migrations/0020_build_reports.sql").read_text()
        for column, added in (("failed_stage", "cluster"), ("failure_reason", "cluster-budget")):
            with self.subTest(column=column):
                constraint = f"build_reports_{column}_check"
                drop = f"ALTER TABLE build_reports DROP CONSTRAINT {constraint};"
                add = f"ALTER TABLE build_reports ADD CONSTRAINT {constraint}"
                self.assertIn(drop, self.text)
                self.assertIn(add, self.text)
                self.assertLess(self.text.index(drop), self.text.index(add))
                old = re.search(rf"{column} text CHECK \({column} IN \((.*?)\)\)", previous, re.S)
                new = re.search(rf"{add}\s+CHECK \({column} IN \((.*?)\)\)", self.text, re.S)
                assert old is not None and new is not None
                values = set(re.findall(r"'([^']+)'", new.group(1)))
                self.assertEqual(values, set(re.findall(r"'([^']+)'", old.group(1))) | {added})
                if column == "failure_reason":
                    self.assertEqual(values, worker.GENERATION_FAILURE_REASONS)
        self.assertNotIn("DROP CONSTRAINT build_reports_check", self.text)
        self.assertNotIn("CHECK ((state = 'failed')", self.text)
        self.assertNotIn("DROP TABLE", self.text)
