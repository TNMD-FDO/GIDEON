"""The host caselaw facade over an in-memory queue and file seam."""

import json
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path

from gideon.host import caselaw, report, stack, staging, worker
from gideon.host.render import worker as identity
from gideon.host.sysio import Command, PathLike, RealHost

RENDERED = "/rendered"
WORK_ROOT = Path("/fictitious-work")
LABEL = "corpus-2099-01-03"
SOURCE = "fictions"
SNAPSHOT = "fictions-2099-01-02"
COURT = "court1"
JOB_ID = 31


class FakeHost(RealHost):
    """Serve psql outcomes and failure files from in-memory values."""

    def __init__(self) -> None:
        super().__init__()
        self.status = "succeeded"
        self.code = 0
        self.counts_output = ""
        self.stderr = "private diagnostic"
        self.run_error: OSError | None = None
        self.calls: list[tuple[list[str], str | None]] = []
        self.files: dict[str, str] = {}

    def run(
        self, argv: Command, *, check: bool = False, input: str | None = None,
        cwd: PathLike | None = None, env: Mapping[str, str] | None = None,
        timeout: float | None = None, passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        self.calls.append((list(argv), input))
        if self.run_error is not None:
            raise self.run_error
        if input is None:
            raise AssertionError("psql must read its statement on stdin")
        if "procrastinate_defer_jobs_v1" in input:
            output = f"{JOB_ID}\n"
        elif "FROM procrastinate_jobs" in input:
            output = f"{JOB_ID}|{self.status}|1\n"
        else:
            output = self.counts_output
        return subprocess.CompletedProcess(list(argv), self.code, output, self.stderr)

    def exists(self, path: PathLike) -> bool:
        return str(path) in self.files

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        try:
            return self.files[str(path)]
        except KeyError as exc:
            raise FileNotFoundError(str(path)) from exc


def failure_for(reason: str, *, job: int = JOB_ID) -> dict[str, object]:
    return {
        "schema": 1, "job": job, "court": COURT, "reason": reason,
        "table": None, "error": None, "at": "2099-01-03T12:00:00+00:00",
    }


def counts_for() -> dict[str, object]:
    return {
        "opinions": 3,
        "by_status": {"ready": 2, "failed": 1},
        "by_text_source": {"xml_harvard": 1, "plain_text": 1},
        "by_precedential": {"published": 1, "unknown": 2},
        "by_failure_reason": {"no-text": 1},
    }


def section_counts_for() -> dict[str, object]:
    return {
        "ready": 2,
        "sectioned": 2,
        "sections_by_type": {"majority": 3, "footnote": 1},
        "chars_by_type": {"majority": 40, "footnote": 5},
    }


def anchor_counts_for() -> dict[str, object]:
    return {
        "ready": 2, "anchored": 2,
        "by_text_source": {
            "xml_harvard": {
                "documents": 1, "with_anchors": 1,
                "chars": 40, "anchored_chars": 30,
            },
            "plain_text": {
                "documents": 1, "with_anchors": 0,
                "chars": 5, "anchored_chars": 0,
            },
        },
    }


def citation_counts_for() -> dict[str, object]:
    return {
        "ready": 2, "cited": 2, "edges": 5,
        "by_type": {"case_cite": 3, "statute": 2},
        "by_form": {"full": 2, "short": 1},
        "case_rows": 3, "case_resolved": 2,
    }


class CaselawHost(unittest.TestCase):
    """Queue arguments, durable failures, and count rows stay bounded and typed."""

    def setUp(self) -> None:
        self.host = FakeHost()
        self.failure_path = str(
            staging.work_directory(LABEL, SOURCE, work_root=WORK_ROOT)
            / f"{COURT}.{identity.CASELAW_FAILURE_NAME}"
        )

    def read_job(self) -> caselaw.CaselawRead | report.Problem:
        return caselaw.read_caselaw(
            self.host, RENDERED, JOB_ID, label=LABEL, snapshot=SNAPSHOT,
            court=COURT, work_root=WORK_ROOT,
        )

    def read_counts(self) -> caselaw.CourtCounts | report.Problem:
        return caselaw.read_counts(
            self.host, RENDERED, source=SOURCE,
            snapshot_date="2099-01-02", court=COURT,
        )

    def read_section_counts(self) -> caselaw.SectionCounts | report.Problem:
        return caselaw.read_section_counts(
            self.host, RENDERED, source=SOURCE,
            snapshot_date="2099-01-02", court=COURT,
        )

    def read_anchor_counts(self) -> caselaw.AnchorCounts | report.Problem:
        return caselaw.read_anchor_counts(
            self.host, RENDERED, source=SOURCE,
            snapshot_date="2099-01-02", court=COURT,
        )

    def read_citation_counts(self) -> caselaw.CitationCounts | report.Problem:
        return caselaw.read_citation_counts(
            self.host, RENDERED, source=SOURCE,
            snapshot_date="2099-01-02", court=COURT,
        )

    def test_defer_binds_the_job_and_lock_on_stdin(self) -> None:
        for limit in (None, 7):
            with self.subTest(limit=limit):
                self.host.calls.clear()
                result = caselaw.defer_caselaw(
                    self.host, RENDERED, label=LABEL, snapshot=SNAPSHOT,
                    court=COURT, limit=limit,
                )
                self.assertEqual(result, JOB_ID)
                self.assertEqual(len(self.host.calls), 1)
                argv, sql = self.host.calls[0]
                self.assertEqual(argv, worker.psql_argv(RENDERED))
                assert sql is not None
                for value in (LABEL, SNAPSHOT, COURT, identity.CASELAW_TASK,
                              identity.CASELAW_QUEUE):
                    self.assertNotIn(value, " ".join(argv))
                    self.assertIn(value, sql)
                self.assertIn(f"\\set v_lock 'caselaw-{LABEL}-{COURT}'", sql)
                self.assertIn("ROW(:'v_queue', :'v_task', 0, :'v_lock', NULL, :'v_args'::jsonb, NULL)", sql)
                args = json.loads(sql.split("\\set v_args '", 1)[1].split("'\n", 1)[0])
                expected: dict[str, object] = {
                    "label": LABEL, "snapshot": SNAPSHOT, "court": COURT,
                }
                if limit is not None:
                    expected["limit"] = limit
                self.assertEqual(args, expected)

    def test_defer_refuses_each_argument_grammar_before_psql(self) -> None:
        base: dict[str, object] = {
            "label": LABEL, "snapshot": SNAPSHOT, "court": COURT, "limit": None,
        }
        cases = (
            ("label", "../other", "corpus-YYYY-MM-DD"),
            ("label", "corpus-2099-13-03", "real date"),
            ("snapshot", "../other", "<source>-YYYY-MM-DD"),
            ("snapshot", "fictions-2099-13-02", "real date"),
            ("court", "Court1", "lowercase court id"),
            ("court", "c" * 33, "32"),
            ("limit", True, "integer"),
            ("limit", 0, "integer"),
            ("limit", identity.CASELAW_LIMIT_MAX + 1, "integer"),
        )
        for field, value, grammar in cases:
            with self.subTest(field=field, value=value):
                self.host.calls.clear()
                result = caselaw.defer_caselaw(
                    self.host, RENDERED, **{**base, field: value},  # type: ignore[arg-type]
                )
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn(field, result.problem)
                self.assertIn(grammar, result.fix)
                self.assertIn(report.command("corpus install"), result.fix)
                self.assertEqual(self.host.calls, [])

    def test_read_waiting_and_succeeded_states(self) -> None:
        for status in ("todo", "doing", "succeeded"):
            with self.subTest(status=status):
                self.host.status = status
                result = self.read_job()
                self.assertIsInstance(result, caselaw.CaselawRead)
                assert isinstance(result, caselaw.CaselawRead)
                self.assertEqual(result.job.status, status)
                self.assertEqual(result.done, status == "succeeded")
                self.assertIsNone(result.failure)
                self.assertIsNone(result.reason)

    def test_each_filed_reason_has_its_specific_fix(self) -> None:
        self.host.status = "failed"
        expected = {
            "missing-stage": (str(WORK_ROOT / LABEL / SOURCE), "corpus install"),
            "stage-mismatch": (str(WORK_ROOT / LABEL / SOURCE), "corpus install"),
            "malformed": ("logs", "corpus install"),
            "store": ("host provision --only disk-layout", "apply", "corpus install"),
            "database": ("host provision", "apply", "corpus install"),
            "local": ("host provision", "apply", "corpus install"),
            "busy": ("Wait", "corpus install"),
            "invalid": ("logs", "corpus install"),
            "segmenter": ("logs", "report the defect", "corpus install"),
            "anchors": ("logs", "report the defect", "corpus install"),
            "citations": ("python3 -m tools.imagebuild gideon --check", "logs", "corpus install"),
            "text-mismatch": ("logs", "new corpus cut", "corpus cut", "corpus install"),
        }
        self.assertEqual(set(expected), identity.CASELAW_FAILURE_REASONS)
        for reason, fix_parts in expected.items():
            with self.subTest(reason=reason):
                self.host.files[self.failure_path] = json.dumps(failure_for(reason))
                result = self.read_job()
                self.assertIsInstance(result, caselaw.CaselawRead)
                assert isinstance(result, caselaw.CaselawRead)
                self.assertFalse(result.done)
                self.assertEqual(result.reason, reason)
                assert result.failure is not None
                for part in fix_parts:
                    self.assertIn(part, result.failure.fix)
                if reason in {"missing-stage", "stage-mismatch"}:
                    self.assertIn("Remove", result.failure.fix)
                if reason == "citations":
                    self.assertEqual(
                        result.failure.fix,
                        "Run python3 -m tools.imagebuild gideon --check and "
                        f"{stack.logs_fix(RENDERED, identity.WORKER_SERVICE_NAME)}, "
                        f"then run {report.command('corpus install')} again.",
                    )

    def test_terminal_status_without_this_jobs_file_is_local(self) -> None:
        for status in ("failed", "aborted", "cancelled"):
            for filed in (None, failure_for("store", job=JOB_ID + 1)):
                with self.subTest(status=status, filed=filed):
                    self.host.status = status
                    self.host.files.clear()
                    if filed is not None:
                        self.host.files[self.failure_path] = json.dumps(filed)
                    result = self.read_job()
                    self.assertIsInstance(result, caselaw.CaselawRead)
                    assert isinstance(result, caselaw.CaselawRead)
                    self.assertEqual(result.reason, "local")
                    self.assertFalse(result.done)
                    assert result.failure is not None
                    self.assertIn("host provision", result.failure.fix)

    def test_matching_failure_file_validates_every_field(self) -> None:
        self.host.status = "failed"
        base = failure_for("malformed")
        cases = (
            ("schema", True), ("schema", 2), ("job", float(JOB_ID)),
            ("court", "othercourt"), ("court", 3),
            ("reason", "private detail"), ("table", "unexpected"),
            ("error", "Value Error"), ("at", "2099-01-03T12:00:00"),
            ("at", "2099-99-03T12:00:00+00:00"),
        )
        for field, invalid in cases:
            with self.subTest(field=field, invalid=invalid):
                value = {**base, field: invalid}
                self.host.files[self.failure_path] = json.dumps(value)
                result = self.read_job()
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn("failure file is invalid", result.problem)
        for value in ({**base, "extra": 1}, {k: v for k, v in base.items() if k != "at"}):
            self.host.files[self.failure_path] = json.dumps(value)
            self.assertIsInstance(self.read_job(), report.Problem)
        self.host.files[self.failure_path] = "not json"
        self.assertIsInstance(self.read_job(), report.Problem)
        self.host.files[self.failure_path] = "[]"
        self.assertIsInstance(self.read_job(), report.Problem)

    def test_read_counts_binds_values_and_parses_one_json_row(self) -> None:
        value = counts_for()
        self.host.counts_output = json.dumps(value) + "\n"
        result = self.read_counts()
        self.assertEqual(result, caselaw.CourtCounts(
            3, {"ready": 2, "failed": 1},
            {"xml_harvard": 1, "plain_text": 1},
            {"published": 1, "unknown": 2}, {"no-text": 1},
        ))
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        for name, expected in (("v_source", SOURCE),
                               ("v_snapshot_date", "2099-01-02"),
                               ("v_court", COURT)):
            self.assertIn(worker.bind(name, expected), sql)
            self.assertNotIn(expected, " ".join(argv))
        self.assertIn(caselaw.CASELAW_COUNTS_SQL, sql)
        self.assertIn("JOIN public.opinions", sql)
        self.assertIn("text_source IS NOT NULL", sql)
        self.assertIn("failure_reason IS NOT NULL", sql)

    def test_read_counts_refuses_each_bad_key_and_value(self) -> None:
        base = counts_for()
        cases = (
            ("opinions", -1), ("opinions", True), ("opinions", "3"),
            ("by_status", {"outside": 1}), ("by_status", {"ready": -1}),
            ("by_status", {"ready": True}),
            ("by_text_source", {"html_with_citations": 1}),
            ("by_text_source", {"none": 1}),
            ("by_text_source", {"plain_text": "1"}),
            ("by_precedential", {"Published": 1}),
            ("by_precedential", {"unknown": False}),
            ("by_failure_reason", {"private": 1}),
            ("by_failure_reason", {"empty": -1}),
        )
        for field, invalid in cases:
            with self.subTest(field=field, invalid=invalid):
                self.host.counts_output = json.dumps({**base, field: invalid})
                result = self.read_counts()
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn("counts row is invalid", result.problem)
        for value in ({**base, "extra": 1}, {k: v for k, v in base.items() if k != "opinions"}):
            self.host.counts_output = json.dumps(value)
            self.assertIsInstance(self.read_counts(), report.Problem)
        self.host.counts_output = "not json"
        self.assertIsInstance(self.read_counts(), report.Problem)

    def test_read_counts_refuses_bad_arguments_and_psql_outcomes(self) -> None:
        for field, bad in (("source", "../outside"), ("snapshot_date", "2099-99-02"),
                           ("snapshot_date", "20990102"), ("court", "Court1")):
            with self.subTest(field=field):
                args = {"source": SOURCE, "snapshot_date": "2099-01-02", "court": COURT}
                args[field] = bad
                self.host.calls.clear()
                result = caselaw.read_counts(self.host, RENDERED, **args)
                self.assertIsInstance(result, report.Problem)
                self.assertEqual(self.host.calls, [])
        self.host.code = 127
        result = self.read_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("unavailable", result.problem)
        self.host.code = 3
        result = self.read_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("exit 3", result.problem)
        self.assertIn(self.host.stderr, result.problem)
        self.host.code = 0
        self.host.run_error = FileNotFoundError("private executable path")
        result = self.read_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("FileNotFoundError", result.problem)
        self.assertNotIn("private executable path", result.problem)

    def test_read_section_counts_binds_values_and_parses_one_json_row(self) -> None:
        self.host.counts_output = json.dumps(section_counts_for()) + "\n"
        self.assertEqual(self.read_section_counts(), caselaw.SectionCounts(
            2, 2, {"majority": 3, "footnote": 1},
            {"majority": 40, "footnote": 5},
        ))
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        for name, expected in (("v_source", SOURCE),
                               ("v_snapshot_date", "2099-01-02"),
                               ("v_court", COURT)):
            self.assertIn(worker.bind(name, expected), sql)
            self.assertNotIn(expected, " ".join(argv))
        self.assertIn(caselaw.SECTION_COUNTS_SQL, sql)
        self.assertNotIn(caselaw.CASELAW_COUNTS_SQL, sql)
        self.assertIn("public.sections", sql)
        self.assertIn("count(DISTINCT s.doc_id)", sql)
        self.assertIn("sum(s.char_end - s.char_start)", sql)

    def test_read_section_counts_refuses_bad_rows(self) -> None:
        base = section_counts_for()
        cases = (
            ("ready", -1), ("ready", True), ("ready", "2"),
            ("sectioned", -1), ("sectioned", True), ("sectioned", 3),
            ("sections_by_type", {"invented": 1}),
            ("sections_by_type", {"majority": -1}),
            ("sections_by_type", {"majority": True}),
            ("chars_by_type", {"invented": 1}),
            ("chars_by_type", {"majority": "4"}),
        )
        for field, bad in cases:
            with self.subTest(field=field, bad=bad):
                self.host.counts_output = json.dumps({**base, field: bad})
                result = self.read_section_counts()
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn("sections row is invalid", result.problem)
        for value in ({**base, "extra": 1},
                      {key: item for key, item in base.items() if key != "ready"}):
            self.host.counts_output = json.dumps(value)
            self.assertIsInstance(self.read_section_counts(), report.Problem)
        self.host.counts_output = "not json"
        self.assertIsInstance(self.read_section_counts(), report.Problem)

    def test_read_section_counts_refuses_bad_arguments_and_psql_outcomes(self) -> None:
        for field, bad in (("source", "../outside"), ("snapshot_date", "2099-99-02"),
                           ("snapshot_date", "20990102"), ("court", "Court1")):
            with self.subTest(field=field):
                args = {"source": SOURCE, "snapshot_date": "2099-01-02", "court": COURT}
                args[field] = bad
                self.host.calls.clear()
                self.assertIsInstance(
                    caselaw.read_section_counts(self.host, RENDERED, **args), report.Problem,
                )
                self.assertEqual(self.host.calls, [])
        self.host.code = 127
        result = self.read_section_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("unavailable", result.problem)
        self.assertIn("host provision", result.fix)
        self.host.code = 3
        result = self.read_section_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("exit 3", result.problem)
        self.assertIn(self.host.stderr, result.problem)
        self.host.code = 0
        self.host.run_error = FileNotFoundError("private executable path")
        result = self.read_section_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("FileNotFoundError", result.problem)
        self.assertNotIn("private executable path", result.problem)

    def test_read_anchor_counts_binds_values_and_parses_one_json_row(self) -> None:
        self.host.counts_output = json.dumps(anchor_counts_for()) + "\n"
        self.assertEqual(self.read_anchor_counts(), caselaw.AnchorCounts(
            2, 2, {
                "xml_harvard": caselaw.SourceCoverage(1, 1, 40, 30),
                "plain_text": caselaw.SourceCoverage(1, 0, 5, 0),
            },
        ))
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        for name, expected in (("v_source", SOURCE),
                               ("v_snapshot_date", "2099-01-02"),
                               ("v_court", COURT)):
            self.assertIn(worker.bind(name, expected), sql)
            self.assertNotIn(expected, " ".join(argv))
        self.assertIn(caselaw.ANCHOR_COUNTS_SQL, sql)
        self.assertNotIn(caselaw.SECTION_COUNTS_SQL, sql)
        self.assertNotIn(caselaw.CASELAW_COUNTS_SQL, sql)
        self.assertIn("d.status = 'ready'", sql)
        self.assertIn("WHERE anchored_at IS NOT NULL", sql)
        self.assertIn("a.kind = 'reporter_page'", sql)
        self.assertIn("a.attrs ->> 'scheme'", sql)
        self.assertIn("max(chars)", sql)
        self.assertIn("sum(COALESCE(a.chars, 0))", sql)

    def test_read_anchor_counts_refuses_bad_rows_and_bounds(self) -> None:
        base = anchor_counts_for()
        cases: tuple[tuple[str, object], ...] = (
            ("ready", -1), ("ready", True), ("ready", "2"),
            ("anchored", -1), ("anchored", True), ("anchored", 3),
            ("by_text_source", {"outside": {
                "documents": 1, "with_anchors": 0, "chars": 5, "anchored_chars": 0,
            }}),
            ("by_text_source", []),
        )
        for field, bad in cases:
            with self.subTest(field=field, bad=bad):
                self.host.counts_output = json.dumps({**base, field: bad})
                result = self.read_anchor_counts()
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn("anchors row is invalid", result.problem)
                self.assertIn("logs", result.fix)
        for source_row in (
            {"documents": -1, "with_anchors": 0, "chars": 5, "anchored_chars": 0},
            {"documents": True, "with_anchors": 0, "chars": 5, "anchored_chars": 0},
            {"documents": 1, "with_anchors": -1, "chars": 5, "anchored_chars": 0},
            {"documents": 1, "with_anchors": True, "chars": 5, "anchored_chars": 0},
            {"documents": 1, "with_anchors": 2, "chars": 5, "anchored_chars": 0},
            {"documents": 1, "with_anchors": 0, "chars": -1, "anchored_chars": 0},
            {"documents": 1, "with_anchors": 0, "chars": "5", "anchored_chars": 0},
            {"documents": 1, "with_anchors": 0, "chars": 5, "anchored_chars": -1},
            {"documents": 1, "with_anchors": 0, "chars": 5, "anchored_chars": True},
            {"documents": 1, "with_anchors": 0, "chars": 5, "anchored_chars": 6},
            {"documents": 1, "with_anchors": 0, "chars": 5},
            {"documents": 1, "with_anchors": 0, "chars": 5,
             "anchored_chars": 0, "extra": 1},
        ):
            with self.subTest(source_row=source_row):
                self.host.counts_output = json.dumps({
                    **base, "by_text_source": {"xml_harvard": source_row},
                })
                self.assertIsInstance(self.read_anchor_counts(), report.Problem)
        for value in ({**base, "extra": 1},
                      {key: item for key, item in base.items() if key != "ready"}):
            self.host.counts_output = json.dumps(value)
            self.assertIsInstance(self.read_anchor_counts(), report.Problem)
        self.host.counts_output = "not json"
        self.assertIsInstance(self.read_anchor_counts(), report.Problem)

    def test_read_anchor_counts_refuses_bad_arguments_and_psql_outcomes(self) -> None:
        for field, bad in (("source", "../outside"), ("snapshot_date", "2099-99-02"),
                           ("snapshot_date", "20990102"), ("court", "Court1")):
            with self.subTest(field=field):
                args = {"source": SOURCE, "snapshot_date": "2099-01-02", "court": COURT}
                args[field] = bad
                self.host.calls.clear()
                self.assertIsInstance(
                    caselaw.read_anchor_counts(self.host, RENDERED, **args), report.Problem,
                )
                self.assertEqual(self.host.calls, [])
        self.host.code = 127
        result = self.read_anchor_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("unavailable", result.problem)
        self.assertIn("host provision", result.fix)
        self.host.code = 3
        result = self.read_anchor_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("exit 3", result.problem)
        self.assertIn(self.host.stderr, result.problem)
        self.host.code = 0
        self.host.run_error = FileNotFoundError("private executable path")
        result = self.read_anchor_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("FileNotFoundError", result.problem)
        self.assertNotIn("private executable path", result.problem)

    def test_read_citation_counts_binds_values_and_parses_one_json_row(self) -> None:
        self.host.counts_output = json.dumps(citation_counts_for()) + "\n"
        self.assertEqual(self.read_citation_counts(), caselaw.CitationCounts(
            2, 2, 5, {"case_cite": 3, "statute": 2},
            {"full": 2, "short": 1}, 3, 2,
        ))
        self.assertEqual(len(self.host.calls), 1)
        argv, sql = self.host.calls[0]
        self.assertEqual(argv, worker.psql_argv(RENDERED))
        assert sql is not None
        for name, expected in (("v_source", SOURCE),
                               ("v_snapshot_date", "2099-01-02"),
                               ("v_court", COURT)):
            self.assertIn(worker.bind(name, expected), sql)
            self.assertNotIn(expected, " ".join(argv))
        self.assertIn(caselaw.CITATION_COUNTS_SQL, sql)
        self.assertNotIn(caselaw.ANCHOR_COUNTS_SQL, sql)
        self.assertIn("public.citations", sql)
        self.assertIn("citations_parsed_at IS NOT NULL", sql)
        self.assertIn("to_cluster IS NOT NULL", sql)

    def test_read_citation_counts_refuses_bad_rows(self) -> None:
        base = citation_counts_for()
        cases = (
            ("ready", -1), ("ready", True), ("ready", "2"),
            ("cited", -1), ("cited", True), ("cited", 3),
            ("edges", -1), ("edges", True),
            ("by_type", {"invented": 1}), ("by_type", {"statute": -1}),
            ("by_type", {"statute": True}),
            ("by_form", {"invented": 1}), ("by_form", {"full": "2"}),
            ("case_rows", -1), ("case_rows", True),
            ("case_resolved", -1), ("case_resolved", True),
            ("case_resolved", 4),
        )
        for field, bad in cases:
            with self.subTest(field=field, bad=bad):
                self.host.counts_output = json.dumps({**base, field: bad})
                result = self.read_citation_counts()
                self.assertIsInstance(result, report.Problem)
                assert isinstance(result, report.Problem)
                self.assertIn("citations row is invalid", result.problem)
        for value in ({**base, "extra": 1},
                      {key: item for key, item in base.items() if key != "ready"}):
            self.host.counts_output = json.dumps(value)
            self.assertIsInstance(self.read_citation_counts(), report.Problem)
        self.host.counts_output = "not json"
        self.assertIsInstance(self.read_citation_counts(), report.Problem)

    def test_read_citation_counts_refuses_bad_arguments_and_psql_outcomes(self) -> None:
        for field, bad in (("source", "../outside"), ("snapshot_date", "2099-99-02"),
                           ("snapshot_date", "20990102"), ("court", "Court1")):
            with self.subTest(field=field):
                args = {"source": SOURCE, "snapshot_date": "2099-01-02", "court": COURT}
                args[field] = bad
                self.host.calls.clear()
                self.assertIsInstance(
                    caselaw.read_citation_counts(self.host, RENDERED, **args), report.Problem,
                )
                self.assertEqual(self.host.calls, [])
        self.host.code = 127
        result = self.read_citation_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("unavailable", result.problem)
        self.assertIn("host provision", result.fix)
        self.host.code = 3
        result = self.read_citation_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("exit 3", result.problem)
        self.assertIn(self.host.stderr, result.problem)
        self.host.code = 0
        self.host.run_error = FileNotFoundError("private executable path")
        result = self.read_citation_counts()
        self.assertIsInstance(result, report.Problem)
        assert isinstance(result, report.Problem)
        self.assertIn("FileNotFoundError", result.problem)
        self.assertNotIn("private executable path", result.problem)


if __name__ == "__main__":
    unittest.main()
