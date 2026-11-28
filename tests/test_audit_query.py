"""Audit queries over planted rows, using the CLI and a fake Postgres reader."""

import contextlib
import io
import json
import operator
import os
import re
import subprocess
import unittest
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from test_stores import FakeHost as StoreHost
from test_stores import ok

from gideon.cli import main
from gideon.host import auditquery, report, site, stack
from gideon.host.render.worker import WORKER_DATABASE_HOST, WORKER_DATABASE_NAME

SITE_TEXT = (Path(__file__).parent / "fixtures/site/second-office.yaml").read_text(encoding="utf-8")
CONFIG = site.load_site_text(SITE_TEXT).config
assert CONFIG is not None
ZONE = ZoneInfo(CONFIG.office.timezone)
DAY = "2099-07-12"
USER = "fixture-audit-user"
CHAT = "fixture-audit-chat"
KB = "fixture-audit-kb"
OTHER_KB = "fixture-audit-kb-other"
COMPARISONS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "=": operator.eq}


def planted_row(row_id: int, local_time: str, **overrides: object) -> dict[str, Any]:
    at = datetime.fromisoformat(local_time).replace(tzinfo=ZONE).astimezone(UTC)
    return {
        "id": row_id,
        "at": at.isoformat(),
        "run_id": str(UUID(int=row_id)),
        "kind": "research_turn",
        "actor_user_id": None,
        "user_id": USER,
        "chat_id": CHAT,
        "kb_ids": [KB],
        "release": "fixture-release",
        "detail": {
            "result": "allowed",
            "outcome": "complete",
            "route": "answer",
            "support_level": "thin",
            "returned": [
                {"doc_id": "fixture-doc-1", "chunk_id": "fixture-chunk-1"},
                {"doc_id": "fixture-doc-1", "chunk_id": "fixture-chunk-2"},
                {"doc_id": "fixture-doc-2", "chunk_id": "fixture-chunk-3"},
            ],
            "latency_ms": 37.5,
        },
        **overrides,
    }


class FakeHost(StoreHost):
    """Evaluate the emitted predicates against the rows held by the fake record."""

    def __init__(self, rows: Sequence[dict[str, Any]] = (), *, site_text: str = SITE_TEXT, uid: int = 0) -> None:
        self.rows = list(rows)
        self.uid = uid
        super().__init__(self.read_audit, {auditquery._SITE_PATH: site_text})

    def geteuid(self) -> int:
        return self.uid

    def read_audit(self, argv: tuple[str, ...], stdin: str | None) -> subprocess.CompletedProcess[str]:
        assert stdin is not None
        bindings = dict(re.findall(r"^\\set (\w+) '([^']*)'$", stdin, re.MULTILINE))
        statement = [line for line in stdin.splitlines() if not line.startswith("\\set ")]
        projection, table, where, order = statement
        assert projection == "SELECT row_to_json(entry)" and table == "FROM public.audit_log AS entry"
        assert where.startswith("WHERE ") and order == "ORDER BY at, id;"
        matches = self.rows
        for predicate in where.removeprefix("WHERE ").split(" AND "):
            if match := re.fullmatch(r":'(\w+)' = ANY\(kb_ids\)", predicate):
                matches = [row for row in matches if bindings[match[1]] in row["kb_ids"]]
            elif match := re.fullmatch(r"(\w+_id) = :'(\w+)'", predicate):
                matches = [row for row in matches if row[match[1]] == bindings[match[2]]]
            else:
                match = re.fullmatch(r"at (>=|>|<=|<|=) :'([^']+)'::timestamptz", predicate)
                assert match is not None, f"Unexpected audit predicate {predicate!r}"
                bound = datetime.fromisoformat(bindings[match[2]])
                compare = COMPARISONS[match[1]]
                matches = [row for row in matches if compare(datetime.fromisoformat(row["at"]), bound)]
        ordered = sorted(matches, key=lambda row: (datetime.fromisoformat(row["at"]), row["id"]))
        return ok(argv, "".join(f"{json.dumps(row)}\n" for row in ordered))


def invoke(host: FakeHost, *flags: str, installed: bool = False) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    environment = {report.GIDEON_INSTALLED_COMMAND: "1"} if installed else {}
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(report, "_installed_form", False),
        patch.object(auditquery, "RealHost", return_value=host),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        code = main(["audit", "query", *flags])
    return code, out.getvalue(), err.getvalue()


def row_header(row: dict[str, Any]) -> str:
    moment = datetime.fromisoformat(row["at"]).astimezone(ZONE).isoformat()
    return f"{moment} {row['kind']} user={row['user_id']} chat={row['chat_id']} kb={','.join(row['kb_ids'])}"


def turn_line(row: dict[str, Any]) -> str:
    detail = row["detail"]
    return (
        f"{row_header(row)} result={detail['result']} outcome={detail['outcome']} route={detail['route']} "
        f"support_level={detail['support_level']} documents=2 chunks=3 latency_ms={detail['latency_ms']}"
    )


class AuditQuery(unittest.TestCase):
    def test_user_and_chat_queries_include_since_and_exclude_until_at_the_minute(self) -> None:
        start = planted_row(18, f"{DAY} 09:00")
        same_minute = planted_row(16, f"{DAY} 09:00", chat_id="fixture-other-chat")
        same_chat = planted_row(12, f"{DAY} 10:15", user_id="fixture-other-user")
        host = FakeHost([
            start,
            planted_row(13, f"{DAY} 11:00"),
            planted_row(14, f"{DAY} 08:59:59"),
            planted_row(15, f"{DAY} 09:30", actor_user_id=USER, user_id="fixture-other-user", chat_id="fixture-other-chat"),
            same_chat,
            same_minute,
        ])
        for flag, value, expected in (("--user", USER, [same_minute, start]), ("--chat", CHAT, [start, same_chat])):
            with self.subTest(selector=flag):
                bounds = ("--since", f"{DAY} 09:00", "--until", f"{DAY} 11:00")
                code, out, err = invoke(host, flag, value, *bounds)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out.splitlines(), [*(turn_line(row) for row in expected), "2 audit rows."])
                code, out, err = invoke(host, flag, value, *bounds, "--json")
                self.assertEqual((code, err, json.loads(out)), (0, "", expected))

    def test_date_only_bounds_include_the_whole_office_day(self) -> None:
        midnight = planted_row(22, f"{DAY} 00:00")
        late = planted_row(21, f"{DAY} 23:45")
        host = FakeHost([
            late,
            planted_row(20, "2099-07-13 00:00"),
            planted_row(23, "2099-07-11 23:59:59"),
            midnight,
        ])
        code, out, err = invoke(host, "--user", USER, "--since", DAY, "--until", DAY, "--json")
        self.assertEqual((code, err, json.loads(out)), (0, "", [midnight, late]))

    def test_matching_kinds_are_all_read_and_json_keeps_every_field(self) -> None:
        turn = planted_row(31, f"{DAY} 08:00")
        denial = planted_row(
            32, f"{DAY} 08:01", kind="conflicts_wall_denial",
            detail={"result": "denied", "reason": "kb_not_readable", "failures": [{"kb_id": KB, "reply": 403}]},
        )
        other = planted_row(
            33, f"{DAY} 08:02", kind="fixture-future-audit-kind", actor_user_id="fixture-actor",
            detail={"result": "fixture-result", "reason": "fixture-reason", "nested": {"fixture-count": 7}},
        )
        host = FakeHost([other, {**denial, "id": 34, "kb_ids": [OTHER_KB]}, denial, turn])
        code, out, err = invoke(host, "--kb", KB)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            turn_line(turn), row_header(denial) + " result=denied reason=kb_not_readable",
            row_header(other), "3 audit rows.",
        ])
        code, out, err = invoke(host, "--kb", KB, "--json")
        self.assertEqual((code, err, json.loads(out)), (0, "", [turn, denial, other]))

    def test_selectors_intersect_and_reach_the_metrics_reader_on_stdin_only(self) -> None:
        match = planted_row(41, f"{DAY} 09:00", kb_ids=[OTHER_KB, KB])
        host = FakeHost([
            planted_row(42, f"{DAY} 09:01", kb_ids=[OTHER_KB]),
            planted_row(43, f"{DAY} 09:02", user_id="fixture-other-user", actor_user_id=USER),
            planted_row(44, f"{DAY} 09:03", chat_id="fixture-other-chat"),
            match,
        ])
        code, out, err = invoke(host, "--kb", KB, "--user", USER, "--chat", CHAT, "--json")
        self.assertEqual((code, err, json.loads(out)), (0, "", [match]))
        (argv, stdin), = host.calls
        self.assertEqual(argv, tuple(stack.exec_argv(
            auditquery._RENDERED_DIR, WORKER_DATABASE_HOST, "psql", "-U", "gideon_ro_metrics",
            "-d", WORKER_DATABASE_NAME, "-X", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=sqlstate", "-tA", "-f", "-",
        )))
        assert stdin is not None
        for name, value in (("kb", KB), ("user", USER), ("chat", CHAT)):
            with self.subTest(selector=name):
                self.assertIn(f"\\set v_{name} '{value}'", stdin)
                self.assertNotIn(value, " ".join(argv))
        self.assertIn("FROM public.audit_log", stdin)
        self.assertIn("WHERE :'v_kb' = ANY(kb_ids) AND user_id = :'v_user' AND chat_id = :'v_chat'", stdin)

    def test_no_matches_are_a_success_in_both_output_forms(self) -> None:
        host = FakeHost([planted_row(51, f"{DAY} 09:00", kb_ids=[OTHER_KB])])
        self.assertEqual(invoke(host, "--kb", KB), (0, "No audit rows matched.\n", ""))
        self.assertEqual(invoke(host, "--kb", KB, "--json"), (0, "[]\n", ""))

    def assert_refusal(
        self, host: FakeHost, flags: tuple[str, ...], problem: str, fix: str, *, reads: int = 0, root: bool = True,
    ) -> None:
        for installed in (False, True):
            with self.subTest(installed=installed):
                host.calls.clear()
                code, out, err = invoke(host, *flags, installed=installed)
                self.assertEqual((code, out), (1, ""))
                refusal, = err.splitlines()
                detail, separator, actual_fix = refusal.partition(" Fix: ")
                self.assertEqual(separator, " Fix: ")
                self.assertTrue(detail.startswith("gideon audit query: "))
                self.assertIn(problem, detail)
                if not installed:
                    prefix = "sudo python3 -m gideon " if root else "python3 -m gideon "
                    self.assertEqual(actual_fix, fix.replace("gideon ", prefix, 1))
                else:
                    self.assertEqual(actual_fix, fix)
                self.assertEqual(len(host.calls), reads)

    def test_root_and_site_refusals_have_fixes_before_any_read(self) -> None:
        self.assert_refusal(
            FakeHost(uid=1234), ("--user", USER), "root is required.",
            "Run gideon audit query as root, for example with sudo.", root=False,
        )
        host = FakeHost(site_text=SITE_TEXT.replace(ZONE.key, "Fictitious/Unknown"))
        self.assert_refusal(
            host, ("--user", USER), "office.timezone",
            "Correct the site file, then run gideon audit query again.",
        )
        with patch.object(auditquery, "ZoneInfo", side_effect=ZoneInfoNotFoundError):
            self.assert_refusal(
                FakeHost(), ("--user", USER), "the office timezone could not be loaded.",
                f"Install timezone data or correct office.timezone in {auditquery._SITE_PATH}, then run gideon audit query again.",
            )

    def test_missing_and_malformed_selectors_have_fixes(self) -> None:
        host = FakeHost()
        self.assert_refusal(
            host, (), "at least one of --kb, --user, or --chat is required.",
            "Run gideon audit query with a knowledge-base, user, or chat id.",
        )
        for flag, value in (("--kb", "fictitious kb"), ("--user", ""), ("--chat", "x" * 129)):
            with self.subTest(flag=flag):
                self.assert_refusal(
                    host, (flag, value), f"{flag} must be a valid id.",
                    f"Run gideon audit query with a valid {flag} id.",
                )

    def test_window_refusals_have_distinct_problems_and_fixes(self) -> None:
        host = FakeHost()
        form = "in YYYY-MM-DD or YYYY-MM-DD HH:MM form."
        for flag, timestamp, problem, correction in (
            ("--since", "2099-02-30", "must be an office time", f"with --since {form}"),
            ("--until", f"{DAY} 12:00:00", "must be an office time", f"with --until {form}"),
            ("--since", "2025-03-09 02:30", "does not exist in office time", "with an unambiguous office time for --since."),
            ("--until", "2025-11-02 01:30", "occurs twice in office time", "with an unambiguous office time for --until."),
        ):
            with self.subTest(flag=flag, timestamp=timestamp):
                self.assert_refusal(
                    host, ("--chat", CHAT, flag, timestamp), f"{flag} {problem}",
                    f"Run gideon audit query {correction}",
                )
        self.assert_refusal(
            host, ("--chat", CHAT, "--since", f"{DAY} 12:01", "--until", f"{DAY} 12:00"),
            "--until is before --since.", "Run gideon audit query with a later --until or an earlier --since.",
        )

    def test_reader_refusals_keep_diagnostics_out_and_give_the_fixes(self) -> None:
        host = FakeHost()
        flags = ("--user", USER)
        status_fix = "Check the Postgres service with gideon status, then retry."
        diagnostic = f"Fictitious reader diagnostic for {USER}"
        output = json.dumps(planted_row(61, f"{DAY} 09:00"))
        for result, problem, fix in (
            (subprocess.CompletedProcess([], 3, output, diagnostic), "audit log reader failed: exit 3.", status_fix),
            (subprocess.CompletedProcess([], 3, output, "psql:<stdin>:6: ERROR:  42P01\n"),
             "the audit log table is missing.", "Run gideon apply, then retry."),
            (subprocess.CompletedProcess([], 0, diagnostic, ""), "audit log reader returned an unreadable row.", status_fix),
        ):
            with self.subTest(problem=problem):
                host.respond = Mock(return_value=result)
                self.assert_refusal(host, flags, problem, fix, reads=1)
                self.assertNotIn(USER, invoke(host, *flags)[2])
        host.respond = Mock(side_effect=FileNotFoundError(diagnostic))
        self.assert_refusal(host, flags, "audit log reader failed: command could not run.", status_fix, reads=1)

    def test_missing_flag_value_exits_two_before_any_read(self) -> None:
        host = FakeHost()
        with (
            patch.object(auditquery, "RealHost", return_value=host),
            patch.object(report, "_installed_form", False),
            contextlib.redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as raised,
        ):
            main(["audit", "query", "--user"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--user", err.getvalue())
        self.assertEqual(host.calls, [])
