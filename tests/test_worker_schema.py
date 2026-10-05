"""Installed Procrastinate schema inputs held for the queue migration."""

import hashlib
import importlib.metadata
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "migrations/0007_procrastinate_queue.sql"
GRANTS_MARKER = b"-- The worker role uses the queue tables and sequences.\n"
SCHEMA_SHA256 = "c70ec4b400a60ad9592787653aae5ae77ae41bf801d56b5bd2712751a07f2009"
NEWEST_MIGRATION = "03.04.00_50_post_add_retry_failed_job_procedure.sql"
_BUMP_FIX = (
    "The installed Procrastinate wheel's schema moved. Write the next GIDEON "
    "migration from the wheel's migration files above the old version, in "
    "filename order, never editing the applied queue migration, then record "
    "the new digest and newest file name here."
)


class WorkerSchemaWheel(unittest.TestCase):
    def _migration_schema_span(self) -> bytes:
        migration = MIGRATION.read_bytes()
        wheel = importlib.metadata.distribution("procrastinate")
        header = (
            f"-- Procrastinate {wheel.version}: the schema below is the installed wheel's\n"
            "-- procrastinate/sql/schema.sql, copied unchanged.\n"
        ).encode()
        self.assertTrue(
            migration.startswith(header),
            "Fix: restore the migration header that identifies the wheel schema.",
        )
        span, marker, _ = migration[len(header):].partition(GRANTS_MARKER)
        self.assertEqual(
            marker, GRANTS_MARKER,
            "Fix: keep worker grants after the copied wheel schema.",
        )
        return span

    def test_installed_schema_and_latest_migration_match_reviewed_wheel(self) -> None:
        wheel = importlib.metadata.distribution("procrastinate")
        schema = Path(str(wheel.locate_file("procrastinate/sql/schema.sql")))
        migrations = Path(str(wheel.locate_file("procrastinate/sql/migrations")))
        self.assertEqual(
            hashlib.sha256(schema.read_bytes()).hexdigest(), SCHEMA_SHA256, _BUMP_FIX
        )
        self.assertEqual(
            max(path.name for path in migrations.iterdir() if path.name.endswith(".sql")),
            NEWEST_MIGRATION,
            _BUMP_FIX,
        )

    def test_migration_copies_the_installed_schema_without_changes(self) -> None:
        wheel = importlib.metadata.distribution("procrastinate")
        schema = Path(str(wheel.locate_file("procrastinate/sql/schema.sql")))
        self.assertEqual(
            self._migration_schema_span(), schema.read_bytes(),
            "Fix: copy the installed wheel's schema.sql byte for byte before the grants.",
        )

    def test_defer_signature_and_composite_field_order(self) -> None:
        span = self._migration_schema_span().decode("utf-8")
        self.assertIn(
            "CREATE FUNCTION procrastinate_defer_jobs_v1(\n"
            "    jobs procrastinate_job_to_defer_v1[]\n"
            ")\n    RETURNS bigint[]",
            span,
            "Fix: restore the installed wheel's defer function signature.",
        )
        composite = re.search(
            r"CREATE TYPE procrastinate_job_to_defer_v1 AS \((.*?)\);", span, re.S
        )
        self.assertIsNotNone(
            composite, "Fix: restore the installed wheel's defer composite type."
        )
        assert composite is not None
        self.assertEqual(
            tuple(re.findall(r"^\s*(\w+)\s+", composite.group(1), re.M)),
            ("queue_name", "task_name", "priority", "lock", "queueing_lock", "args", "scheduled_at"),
            "Fix: restore the defer composite's field order from the installed wheel.",
        )


if __name__ == "__main__":
    unittest.main()
