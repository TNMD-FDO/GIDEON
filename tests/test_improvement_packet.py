"""Behavioral coverage for the text-only candidate packet boundary."""

import contextlib
import io
import json
import os
import stat
import subprocess
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

from gideon import guardrail
from gideon.api import stamp
from gideon.cli import main
from gideon.evaluation import record
from gideon.host import backupset, stores
from gideon.host.report import Problem
from gideon.host.sysio import Command, CompletedText, Host, PathLike
from gideon.improvement import owuisnapshot, packet
from gideon.improvement.feedback import FeedbackRecord
from gideon.improvement.snapshot import RatedTurn, SnapshotReading, SnapshotSource

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests/fixtures/frontend/feedback-snapshot.jsonl"
SITE_PATH = Path("/etc/gideon/site.yaml")
OUTPUT_PATH = Path("/tmp/fictional-packet/output")
SITE_TEXT = (ROOT / "config/site.example.yaml").read_text(encoding="utf-8")
ZONE = "America/Chicago"
MONTH = "2023-11"


def _fixture_rows() -> list[dict[str, object]]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


def _turn(
    answer: str | None,
    *,
    answer_role: str | None = "assistant",
    question: str | None = "FICTIONAL_QUESTION_SENTINEL_unit",
    identifier: str = "fictional-message-unit",
) -> RatedTurn:
    return RatedTurn(
        FeedbackRecord("down", "fictional-chat-unit", identifier, "fictional-model", 7),
        f"fictional-feedback-{identifier}",
        question,
        answer,
        answer_role,
    )


class FakeHost:
    """A dictionary-backed host that records packet writes and subprocess input."""

    def __init__(self, *, euid: int = 0, site_text: str | None = SITE_TEXT) -> None:
        self.euid = euid
        self.nodes: dict[str, tuple[int, int]] = {}
        self.files: dict[str, str] = {}
        self.reads: list[str] = []
        self.events: list[tuple[object, ...]] = []
        self.run_calls: list[tuple[tuple[str, ...], str | None]] = []
        if site_text is not None:
            self.nodes[str(SITE_PATH)] = (stat.S_IFREG | 0o600, 0)
            self.files[str(SITE_PATH)] = site_text

    def geteuid(self) -> int:
        return self.euid

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        self.reads.append(key)
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
        parent = str(Path(key).parent)
        if parent not in self.nodes:
            raise FileNotFoundError(parent)
        self.events.append(("write_text", key, mode))
        self.nodes[key] = (stat.S_IFREG | mode, 0)
        self.files[key] = text

    def exists(self, path: PathLike) -> bool:
        key = os.fspath(path)
        return key in self.nodes or key in self.files

    def listdir(self, path: PathLike) -> list[str]:
        base = Path(path)
        if not self.exists(base):
            raise FileNotFoundError(os.fspath(path))
        names = {
            Path(key).name
            for key in (*self.nodes.keys(), *self.files.keys())
            if Path(key).parent == base
        }
        return sorted(names)

    def stat(self, path: PathLike) -> os.stat_result:
        key = os.fspath(path)
        if key not in self.nodes:
            raise FileNotFoundError(key)
        mode, uid = self.nodes[key]
        return cast(os.stat_result, SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=0))

    def chmod(self, path: PathLike, mode: int) -> None:
        key = os.fspath(path)
        current, uid = self.nodes[key]
        self.nodes[key] = (stat.S_IFMT(current) | mode, uid)
        self.events.append(("chmod", key, mode))

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
        if self.exists(key):
            raise FileExistsError(key)
        self.nodes[key] = (stat.S_IFDIR | mode, 0)
        self.events.append(("mkdir", key, mode))

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: object | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> CompletedText:
        del check, cwd, env, timeout, passthrough
        self.run_calls.append((tuple(argv), input))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def directory(self, path: PathLike, *, uid: int = 0, mode: int = 0o755) -> None:
        self.nodes[os.fspath(path)] = (stat.S_IFDIR | mode, uid)


class FixtureSource:
    """Decode only fixture rows whose timestamps fall inside the read window."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.windows: list[tuple[int, int]] = []

    def read(self, start: int, end: int) -> SnapshotReading:
        self.windows.append((start, end))
        lines = []
        for row in self.rows:
            created_at = row.get("created_at")
            if (
                isinstance(created_at, int)
                and not isinstance(created_at, bool)
                and start <= created_at < end
            ):
                lines.append(json.dumps(row, ensure_ascii=False))
        return owuisnapshot.decode_lines(lines)


def _filtered_reading(rows: list[dict[str, object]]) -> SnapshotReading:
    start, end = packet.month_bounds(MONTH, ZONE)
    return FixtureSource(rows).read(start, end)


def _bucketed_down(reading: SnapshotReading) -> tuple[packet.BucketedTurn, ...]:
    return tuple(
        packet.BucketedTurn(turn, *packet.bucket_for(turn))
        for turn in reading.turns
        if turn.record.rating == "down"
    )


class BucketsAndMonths(unittest.TestCase):
    def test_each_fixed_refusal_alone_and_after_a_released_prefix(self) -> None:
        for family, refusal_text in guardrail.REFUSAL_BY_FAMILY.items():
            with self.subTest(family=family):
                self.assertEqual(packet.bucket_for(_turn(refusal_text)), ("refused", family))
                prefixed = "FICTIONAL_RELEASED_PREFIX_SENTINEL" + guardrail.REFUSAL_SEPARATOR + refusal_text
                self.assertEqual(packet.bucket_for(_turn(prefixed)), ("refused", family))

    def test_refusal_glued_to_prior_text_is_not_a_refusal(self) -> None:
        refusal_text = next(iter(guardrail.REFUSAL_BY_FAMILY.values()))
        glued = "FICTIONAL_RELEASED_PREFIX_SENTINEL" + refusal_text
        self.assertEqual(packet.bucket_for(_turn(glued)), ("answered", None))

    def test_stamp_plain_empty_absent_and_non_assistant_messages(self) -> None:
        self.assertEqual(
            packet.bucket_for(_turn("FICTIONAL_STAMPED_ANSWER" + stamp.STAMP_TAIL)),
            ("stamped", None),
        )
        self.assertEqual(packet.bucket_for(_turn("FICTIONAL_PLAIN_ANSWER")), ("answered", None))
        self.assertEqual(packet.bucket_for(_turn("")), ("unreadable", None))
        self.assertEqual(packet.bucket_for(_turn("  \n  ")), ("unreadable", None))
        self.assertEqual(packet.bucket_for(_turn(None)), ("unreadable", None))
        self.assertEqual(packet.bucket_for(_turn("FICTIONAL_USER_MESSAGE", answer_role="user")), ("unreadable", None))

    def test_month_bounds_use_dst_offsets_and_default_month_uses_local_year(self) -> None:
        zone = ZoneInfo(ZONE)
        expected_start = int(datetime(2023, 11, 1, tzinfo=zone).timestamp())
        expected_end = int(datetime(2023, 12, 1, tzinfo=zone).timestamp())
        self.assertEqual(packet.month_bounds(MONTH, ZONE), (expected_start, expected_end))
        self.assertEqual(expected_end - expected_start, 30 * 24 * 60 * 60 + 60 * 60)

        year_boundary = datetime(2024, 1, 1, 0, 30, tzinfo=UTC).timestamp()
        self.assertEqual(packet.default_month(ZONE, year_boundary), "2023-11")
        january_local = datetime(2024, 1, 15, 12, tzinfo=zone).timestamp()
        self.assertEqual(packet.default_month(ZONE, january_local), "2023-12")


class OutputPath(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.checkout = Path("/tmp/fictional-checkout")

    def test_every_inventory_root_staging_and_install_home_are_refused(self) -> None:
        paths = [
            Path(root.source) / "fictional-candidate-output"
            for root in backupset.inventory_roots(os.fspath(self.checkout))
        ]
        paths.extend(
            (
                Path(backupset.STAGING) / "fictional-candidate-output",
                Path("/opt/gideon/fictional-candidate-output"),
            )
        )
        for path in paths:
            with self.subTest(path=path):
                result = packet.judge_out(path, self.checkout, cast(Host, self.host))
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("then retry", result.fix)
        self.assertEqual(self.host.events, [])

    def test_relative_git_tree_nonempty_path_and_file_are_refused(self) -> None:
        relative = packet.judge_out("fictional-relative-output", self.checkout, cast(Host, self.host))
        self.assertIsInstance(relative, Problem)
        assert isinstance(relative, Problem)
        self.assertIn("absolute", relative.problem)

        work_tree = Path("/root/fictional-work-tree")
        self.host.directory(work_tree / ".git")
        result = packet.judge_out(work_tree / "nested" / "candidates", self.checkout, cast(Host, self.host))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("checkout", result.problem)

        nonempty = Path("/tmp/fictional-packet/nonempty")
        self.host.directory(nonempty)
        self.host.nodes[str(nonempty / "note.txt")] = (stat.S_IFREG | 0o600, 0)
        result = packet.judge_out(nonempty, self.checkout, cast(Host, self.host))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("not empty", result.problem)

        file_path = Path("/tmp/fictional-packet/not-a-directory")
        self.host.nodes[str(file_path)] = (stat.S_IFREG | 0o600, 0)
        result = packet.judge_out(file_path, self.checkout, cast(Host, self.host))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("not an empty directory", result.problem)

    def test_an_alias_of_a_protected_directory_is_judged_as_that_directory(self) -> None:
        protected = Path(backupset.STAGING) / "fictional-candidate-output"
        alias = Path("/root/fictional-alias/candidates")

        def resolve(path: str) -> str:
            return os.fspath(protected) if path == os.fspath(alias) else path

        result = packet.judge_out(alias, self.checkout, cast(Host, self.host), resolve)
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("backup set", result.problem)
        self.assertIsNone(packet.judge_out(alias, self.checkout, cast(Host, self.host), lambda path: path))

    def test_unowned_empty_directory_is_refused_and_absent_or_root_owned_is_accepted(self) -> None:
        unowned = Path("/tmp/fictional-packet/unowned")
        self.host.directory(unowned, uid=41)
        result = packet.judge_out(unowned, self.checkout, cast(Host, self.host))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("root-owned", result.problem)
        self.assertIn("Create the directory as root", result.fix)

        absent = Path("/tmp/fictional-packet/absent")
        self.assertIsNone(packet.judge_out(absent, self.checkout, cast(Host, self.host)))
        empty = Path("/tmp/fictional-packet/empty")
        self.host.directory(empty, uid=0)
        self.assertIsNone(packet.judge_out(empty, self.checkout, cast(Host, self.host)))


class SnapshotAdapter(unittest.TestCase):
    def test_argv_statement_projection_and_stdin_boundary(self) -> None:
        command = owuisnapshot.argv("/tmp/fictional-rendered")
        self.assertIn("-T", command)
        self.assertEqual(command[command.index("-U") + 1], owuisnapshot.SUPERUSER)
        self.assertEqual(command[command.index("-d") + 1], owuisnapshot.FRONTEND_DATABASE)

        sql = owuisnapshot.statement(100, 200)
        for selected in (
            "data->'rating'",
            "data->>'model_id'",
            "meta->>'chat_id'",
            "meta->>'message_id'",
            "snapshot->'chat'->'chat'->'history'->'messages'",
            "rated_message->>'parentId'",
            "rated_message->>'content'",
            "rated_message->>'role'",
        ):
            with self.subTest(selected=selected):
                self.assertIn(selected, sql)
        for excluded in ("comment", "tags", "user_id", "reason", "message_index", "sibling_model_ids"):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, sql)
        self.assertNotIn("data->>'reason'", sql)

        host = FakeHost()
        reading = owuisnapshot.read(cast(Host, host), "/tmp/fictional-rendered", 100, 200)
        self.assertEqual(reading, SnapshotReading((), 0))
        self.assertEqual(len(host.run_calls), 1)
        argv, stdin = host.run_calls[0]
        self.assertEqual(stdin, sql)
        self.assertNotIn(sql, argv)

    def test_decode_lines_counts_bad_rows_and_constant_values_follow_registries(self) -> None:
        lines = FIXTURE.read_text(encoding="utf-8").splitlines()
        decoded = owuisnapshot.decode_lines(lines)
        self.assertEqual(decoded.skipped, 2)
        self.assertEqual(len(decoded.turns), len(lines) - decoded.skipped)

        frontend_database = next(
            database for database in stores.DATABASE_SPECS if database.owner == "openwebui"
        )
        self.assertEqual(owuisnapshot.SUPERUSER, stores._SUPERUSER)
        self.assertEqual(owuisnapshot.FRONTEND_DATABASE, frontend_database.name)
        self.assertEqual(owuisnapshot.POSTGRES_SERVICE, record.POSTGRES_SERVICE)


class PageAndManifest(unittest.TestCase):
    def test_fixture_build_is_stable_sorted_text_only_and_uses_utc_times(self) -> None:
        rows = _fixture_rows()
        reading = _filtered_reading(rows)
        turns = _bucketed_down(reading)
        page = packet.render_page(MONTH, ZONE, reading, turns)
        metadata = packet.manifest(MONTH, ZONE, reading, turns, page)
        reversed_reading = _filtered_reading(list(reversed(rows)))
        reversed_turns = _bucketed_down(reversed_reading)
        reversed_page = packet.render_page(MONTH, ZONE, reversed_reading, reversed_turns)
        reversed_metadata = packet.manifest(MONTH, ZONE, reversed_reading, reversed_turns, reversed_page)

        self.assertEqual(page, reversed_page)
        self.assertEqual(
            json.dumps(metadata, sort_keys=True, indent=2),
            json.dumps(reversed_metadata, sort_keys=True, indent=2),
        )
        self.assertNotIn("fictional-feedback-outside", page)
        self.assertEqual(metadata["ratings"], len(reading.turns))
        self.assertEqual(metadata["skipped"], reading.skipped)
        page_details = cast(dict[str, object], metadata["page"])
        self.assertEqual(page_details["name"], packet.page_name(MONTH))
        self.assertEqual(page_details["lines"], len(page.splitlines()))

        heading_lines = [line for line in page.splitlines() if line.startswith("== turn ")]
        actual_order: list[tuple[str, str]] = []
        page_lines = page.splitlines()
        for heading in heading_lines:
            section_index = page_lines.index(heading)
            bucket = heading.split("|")[1].strip().split()[0]
            message = next(
                part.strip().removeprefix("message: ")
                for part in page_lines[section_index + 1].split("|")
                if part.strip().startswith("message:")
            )
            actual_order.append((bucket, message))
            rated = heading.rsplit("rated ", 1)[1]
            self.assertTrue(rated.endswith("Z"))
            self.assertRegex(rated, r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
        order = {bucket: index for index, bucket in enumerate(packet.BUCKETS)}
        self.assertEqual(actual_order, sorted(actual_order, key=lambda item: (order[item[0]], item[1])))
        self.assertTrue(all(" of " in line for line in heading_lines))

        rendered_manifest = json.dumps(metadata, sort_keys=True, indent=2)
        visible_texts: list[str] = []
        for item in turns:
            if item.bucket != "unreadable":
                if item.turn.question:
                    self.assertIn(item.turn.question, page)
                    visible_texts.append(item.turn.question)
                if item.turn.answer:
                    self.assertIn(item.turn.answer, page)
                    visible_texts.append(item.turn.answer)
        self.assertTrue(any("FICTIONAL_QUESTION_SENTINEL_" in text for text in visible_texts))
        self.assertTrue(any("FICTIONAL_ANSWER_SENTINEL_" in text for text in visible_texts))
        # An unreadable turn shows its ids and two absences, never its texts.
        self.assertNotIn("FICTIONAL_QUESTION_SENTINEL_user_role", page)
        self.assertIn("feedback: fictional-feedback-user-role", page)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            representations = (
                repr(reading),
                repr(turns),
                *(repr(turn) for turn in reading.turns),
                repr(metadata),
            )
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        unselected = str(rows[0]["unselected"])
        sentinels = [unselected, *(text.split()[0] for text in visible_texts if "SENTINEL_" in text)]
        for sentinel in sentinels:
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel, rendered_manifest)
                self.assertNotIn(sentinel, stdout.getvalue())
                self.assertNotIn(sentinel, stderr.getvalue())
                self.assertTrue(all(sentinel not in rendered for rendered in representations))
                if sentinel == unselected:
                    self.assertNotIn(sentinel, page)


class CandidateCommand(unittest.TestCase):
    def _invoke(
        self,
        host: FakeHost,
        source: SnapshotSource,
        *,
        out: str = str(OUTPUT_PATH),
        month: str | None = MONTH,
        include_month: bool = True,
    ) -> tuple[int, str, str]:
        argv = ["eval", "candidates", "--out", out]
        if include_month:
            argv.extend(("--month", "" if month is None else month))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(packet, "RealHost", return_value=host),
            patch.object(owuisnapshot, "source", return_value=source),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_cli_writes_private_files_in_order_for_absent_and_empty_directories(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                host = FakeHost()
                output = OUTPUT_PATH / ("existing" if existing else "new")
                if existing:
                    host.directory(output, uid=0, mode=0o755)
                source = FixtureSource(_fixture_rows())
                result, stdout, stderr = self._invoke(host, source, out=os.fspath(output))

                self.assertEqual(result, 0)
                self.assertEqual(stderr, "")
                rows = stdout.splitlines()
                self.assertEqual([row.split(":", 1)[0] for row in rows], ["load", "read", "write", "summary"])
                self.assertIn("7 rated down", rows[1])
                self.assertIn("2 skipped", rows[1])
                if existing:
                    self.assertEqual(host.events[0], ("chmod", os.fspath(output), 0o700))
                else:
                    self.assertEqual(host.events[0], ("mkdir", os.fspath(output), 0o700))
                self.assertEqual(host.events[1], ("write_text", os.fspath(output / packet.page_name(MONTH)), 0o600))
                self.assertEqual(host.events[2], ("write_text", os.fspath(output / "manifest.json"), 0o600))

                page = host.files[os.fspath(output / packet.page_name(MONTH))]
                serialized = host.files[os.fspath(output / "manifest.json")]
                metadata = json.loads(serialized)
                self.assertEqual(serialized, json.dumps(metadata, sort_keys=True, indent=2) + "\n")
                self.assertIn("FICTIONAL_QUESTION_SENTINEL_", page)
                self.assertIn("FICTIONAL_ANSWER_SENTINEL_", page)
                self.assertNotIn("FICTIONAL_UNSELECTED_SENTINEL", page + serialized + stdout + stderr)
                self.assertNotIn("fictional-feedback-outside", page + serialized)
                for sentinel in (
                    "FICTIONAL_QUESTION_SENTINEL_",
                    "FICTIONAL_ANSWER_SENTINEL_",
                ):
                    for text in (serialized, stdout, stderr):
                        self.assertNotIn(sentinel, text)

    def test_each_preflight_refusal_has_a_fix_and_writes_nothing(self) -> None:
        cases: list[tuple[str, FakeHost, str, str | None, str]] = [
            ("root", FakeHost(euid=1000), str(OUTPUT_PATH), MONTH, "sudo python3 -m gideon eval candidates --out <dir>"),
            ("site", FakeHost(site_text=None), str(OUTPUT_PATH), MONTH, "Correct the site file, then retry."),
            ("month", FakeHost(), str(OUTPUT_PATH), "2023-13", "Supply --month YYYY-MM, then retry."),
            ("output", FakeHost(), "/opt/gideon/candidates", MONTH, "under /root"),
            ("relative", FakeHost(), "fictional-relative", MONTH, "absolute directory under /root"),
        ]
        for name, host, out, month, fix in cases:
            with self.subTest(case=name):
                source = FixtureSource(_fixture_rows())
                result, stdout, stderr = self._invoke(host, source, out=out, month=month)
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertIn(fix, stderr)
                self.assertEqual(host.events, [])

    def test_refused_read_is_a_stage_row_and_writes_nothing(self) -> None:
        class RefusingSource:
            def read(self, start: int, end: int) -> SnapshotReading | Problem:
                del start, end
                return Problem("fictional source unavailable", "Check the stack, then retry.")

        host = FakeHost()
        result, stdout, stderr = self._invoke(host, cast(SnapshotSource, RefusingSource()))
        self.assertEqual(result, 1)
        self.assertEqual(stderr, "")
        self.assertEqual([line.split(":", 1)[0] for line in stdout.splitlines()], ["load", "read"])
        self.assertIn("read: refuse", stdout)
        self.assertIn("Check the stack, then retry.", stdout)
        self.assertEqual(host.events, [])

    def test_empty_month_writes_an_empty_page_and_manifest(self) -> None:
        host = FakeHost()
        empty_source = FixtureSource([])
        result, stdout, stderr = self._invoke(host, empty_source, month="2019-01")
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("summary: ok", stdout)
        page = host.files[os.fspath(OUTPUT_PATH / packet.page_name("2019-01"))]
        self.assertIn("month 2019-01", page)
        self.assertIn("refused 0, stamped 0, answered 0, unreadable 0", page)
        self.assertNotIn("== turn", page)
        # Even an empty page explains itself: every bucket and the runbook are named.
        self.assertIn("How to read this page", page)
        for bucket in packet.BUCKETS:
            self.assertIn(f"    {bucket} ", page)
        self.assertIn("docs/runbooks/feedback-packet.md", page)
        metadata = json.loads(host.files[os.fspath(OUTPUT_PATH / "manifest.json")])
        self.assertEqual(metadata["turns"], [])


if __name__ == "__main__":
    unittest.main()
