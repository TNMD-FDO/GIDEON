"""The upstream office section over the read-only metrics seam."""

import json
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from gideon.evaluation.record import EVAL_DATABASE, METRICS_ROLE, POSTGRES_SERVICE
from gideon.host import stack
from gideon.host.corpus import record
from gideon.host.report import Problem
from gideon.host.sysio import Command, Host, PathLike
from gideon.improvement import upstream
from gideon.improvement.feedback import FeedbackReading
from gideon.improvement.sections import Context, SectionReport, read_rows
from gideon.improvement.triggers import TriggerRegistry

RENDERED = Path("/tmp/fictional-rendered")


class FakeHost:
    """Record the one metrics read and provide fictitious database lines."""

    def __init__(self, lines: tuple[str, ...], returncode: int = 0) -> None:
        self.lines = lines
        self.returncode = returncode
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

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
        self.calls.append((tuple(argv), input))
        return subprocess.CompletedProcess(list(argv), self.returncode, "\n".join(self.lines), "")


def _state(
    source: str,
    *,
    newest_at: str = "2099-01-03T12:00:00+00:00",
    answered_label: str | None = "2099-01-02",
    open_notice: bool = True,
    unanswered_reason: str | None = None,
    pinned_label: str | None = "corpus-2099-01-01",
) -> dict[str, object]:
    """Build one visibly fictitious row in the exported SQL's shape."""

    answered = answered_label is not None
    unanswered = unanswered_reason is not None
    return {
        "source": source,
        "newest_at": newest_at,
        "newest_outcome": "unanswered" if unanswered else "observed",
        "newest_latest_label": None if unanswered else answered_label,
        "newest_effective_date": None,
        "newest_url": "https://fictional.example/index",
        "newest_detail": unanswered_reason,
        "answered_at": ("2099-01-02T12:00:00+00:00" if unanswered else newest_at) if answered else None,
        "answered_outcome": "observed" if answered else None,
        "answered_latest_label": answered_label,
        "answered_effective_date": None,
        "answered_url": "https://fictional.example/index" if answered else None,
        "answered_detail": None,
        "first_seen": "2099-01-02T09:00:00+00:00" if answered else None,
        "open": open_notice if answered else False,
        "pinned_label": pinned_label,
        "pinned_date": pinned_label.removeprefix("corpus-") if pinned_label else None,
        "unanswered_since": "2099-01-03T08:00:00+00:00" if unanswered else None,
    }


class UpstreamSectionCase(unittest.TestCase):
    def _render(self, lines: tuple[str, ...], returncode: int = 0) -> SectionReport | Problem:
        host = FakeHost(lines, returncode)
        context = Context(
            host=cast(Host, host),
            checkout_root=Path("/tmp/fictional-checkout"),
            rendered_dir=RENDERED,
            registry=cast(TriggerRegistry, None),
            build_box=False,
            query=lambda sql: read_rows(cast(Host, host), RENDERED, sql),
            feedback=lambda: FeedbackReading((), 0),
            now=lambda: 0.0,
        )
        result = upstream.UPSTREAM_SECTION.render(context)
        self.assertEqual(len(host.calls), 1)
        argv, statement = host.calls[0]
        self.assertEqual(statement, f"SELECT row_to_json(state) FROM ({record.WATCH_STATE_SQL}) AS state;\n")
        self.assertEqual(argv, tuple(stack.exec_argv(
            RENDERED, POSTGRES_SERVICE, "psql", "-U", METRICS_ROLE, "-d", EVAL_DATABASE,
            "-v", "ON_ERROR_STOP=1", "-tA", "-f", "-",
        )))
        return result

    def test_states_header_and_unanswered_streak(self) -> None:
        lines = (
            json.dumps(_state("fictional-open")),
            json.dumps(_state("fictional-pinned", answered_label="2099-01-01", open_notice=False)),
            json.dumps(_state("fictional-stale", unanswered_reason="timeout")),
            json.dumps(_state("fictional-only-unanswered", answered_label=None, unanswered_reason="worker-unavailable", pinned_label=None)),
        )
        result = self._render(lines)
        self.assertIsInstance(result, SectionReport)
        assert isinstance(result, SectionReport)
        self.assertEqual(result.detail, "4 sources observed, 2 open notices, 2 unanswered sources, newest observation 2099-01-03")
        self.assertEqual(tuple(row.state for row in result.rows), (
            "fired", "not fired", "fired", "not yet measurable",
        ))
        self.assertEqual(result.rows[0].detail,
                         "newest upstream 2099-01-02, newest pinned 2099-01-01 (corpus-2099-01-01), first sighting 2099-01-02")
        self.assertEqual(result.rows[1].detail,
                         "newest upstream 2099-01-01, newest pinned 2099-01-01 (corpus-2099-01-01), first sighting 2099-01-02")
        self.assertIn("newest upstream 2099-01-02", result.rows[2].detail)
        self.assertTrue(result.rows[2].detail.endswith("unanswered since 2099-01-03 (timeout)"))
        self.assertEqual(result.rows[3].detail,
                         "no answered upstream snapshot, unanswered since 2099-01-03 (worker-unavailable)")
        for row in result.rows:
            self.assertNotIn("fictional.example", row.detail)

    def test_empty_and_refused_reads(self) -> None:
        empty = self._render(())
        self.assertEqual(empty, SectionReport(
            "0 sources observed, 0 open notices, 0 unanswered sources, newest observation none", ()
        ))
        refused = self._render((), returncode=8)
        self.assertIsInstance(refused, Problem)
        assert isinstance(refused, Problem)
        self.assertIn("metrics reader failed with exit code 8", refused.problem)

    def test_unreadable_rows_are_counted_without_quoting_them(self) -> None:
        bad_reason = _state("fictional-invalid", unanswered_reason="timeout")
        bad_reason["newest_detail"] = "FICTIONAL_PRIVATE_TEXT"
        bad_source = _state("fictional-invalid")
        bad_source["source"] = "FICTIONAL PRIVATE TEXT"
        bad_date = _state("fictional-invalid")
        bad_date["answered_latest_label"] = "2099-99-99"
        bad_label = _state("fictional-invalid")
        bad_label["pinned_label"] = "FICTIONAL_PRIVATE_TEXT"
        result = self._render(tuple(json.dumps(row) for row in (
            bad_reason, bad_source, bad_date, bad_label,
        )) + ("FICTIONAL_RAW_ROW",))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertEqual(result.problem, "metrics reader returned 5 unreadable upstream rows")
        self.assertIn("Run ", result.fix)
        self.assertNotIn("FICTIONAL_PRIVATE_TEXT", result.problem + result.fix)
        self.assertNotIn("FICTIONAL_RAW_ROW", result.problem + result.fix)


if __name__ == "__main__":
    unittest.main()
