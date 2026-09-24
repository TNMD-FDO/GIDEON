"""Read-only proposals report over the committed trigger registry."""

import argparse
import ast
import contextlib
import io
import os
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn, cast
from unittest.mock import patch

from gideon import cli
from gideon.evaluation import evalset, judgments, record
from gideon.host import owui, report, secrets, stack
from gideon.host.nogpu import BUILD_BOX_PATH, NO_GPU_PATH
from gideon.host.render.owui import FEEDBACK_LIST_ROUTE
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike
from gideon.improvement import proposals, triggers, watch
from gideon.improvement.feedback import FeedbackReading, FeedbackRecord
from gideon.improvement.sections import (
    Context,
    Row,
    Scope,
    SectionReport,
)

ROOT = Path(__file__).resolve().parent.parent
IMPROVEMENT = ROOT / "gideon" / "improvement"
# The CLI cases hold the triggers section alone; the feedback cases below bring its read.
TRIGGERS_ONLY = (watch.TRIGGERS_SECTION,)
REGISTRY_PATH = ROOT / "config" / "triggers.yaml"
RENDERED = Path("/etc/gideon/rendered")
DIAGNOSTIC = "FICTIONAL_DATABASE_DIAGNOSTIC"
QUERY = "SELECT 'fictional query';\n"


def _key(path: PathLike) -> str:
    return os.fspath(path)


def _reader_argv(rendered_dir: PathLike = RENDERED) -> tuple[str, ...]:
    return tuple(
        stack.exec_argv(
            rendered_dir,
            record.POSTGRES_SERVICE,
            "psql",
            "-U",
            record.METRICS_ROLE,
            "-d",
            record.EVAL_DATABASE,
            "-v",
            "ON_ERROR_STOP=1",
            "-tA",
            "-f",
            "-",
        )
    )


class ReadOnlyHost:
    """Dict-backed Host that records reads and rejects every write method."""

    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        directories: Sequence[str] = (),
        run_rc: int = 0,
        run_stdout: str = "",
        run_stderr: str = "",
        run_error: BaseException | None = None,
    ) -> None:
        self.files = dict(files or {})
        self.directories = set(directories)
        for path in (*self.files, *self.directories):
            self.directories.update(parent.as_posix() for parent in Path(path).parents)
        self.run_rc = run_rc
        self.run_stdout = run_stdout
        self.run_stderr = run_stderr
        self.run_error = run_error
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.write_attempted = False

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
        self.calls.append((command, input))
        if not command or command[0] != "docker":
            raise AssertionError(f"unexpected command: {command}")
        if self.run_error is not None:
            raise self.run_error
        return subprocess.CompletedProcess(
            list(command), self.run_rc, self.run_stdout, self.run_stderr
        )

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = _key(path)
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
        del path, text, encoding, mode
        self._refuse_write()

    def exists(self, path: PathLike) -> bool:
        key = _key(path)
        return key in self.files or key in self.directories

    def listdir(self, path: PathLike) -> list[str]:
        key = _key(path).rstrip("/")
        if key not in self.directories:
            raise FileNotFoundError(key)
        prefix = f"{key}/"
        children = {
            child[len(prefix) :].split("/", 1)[0]
            for child in (*self.files, *self.directories)
            if child.startswith(prefix) and child != key
        }
        return sorted(children)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del path, missing_ok
        self._refuse_write()

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        raise FileNotFoundError

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode
        self._refuse_write()

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid
        self._refuse_write()

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok
        self._refuse_write()

    def geteuid(self) -> int:
        return 1000

    def take_lock(self, path: PathLike, record_value: str) -> str | None:
        del path, record_value
        return self._refuse_write()

    def release_lock(self, path: PathLike) -> None:
        del path
        self._refuse_write()

    def _refuse_write(self) -> NoReturn:
        self.write_attempted = True
        raise AssertionError("the proposals report attempted a write")


def _base_host(
    *,
    build_box: bool = False,
    no_gpu: bool = False,
    registry_text: str | None = None,
    directories: Sequence[str] = (),
    run_rc: int = 0,
    run_stdout: str = "",
    run_stderr: str = "",
    run_error: BaseException | None = None,
) -> ReadOnlyHost:
    files = {_key(REGISTRY_PATH): REGISTRY_PATH.read_text(encoding="utf-8")}
    if registry_text is not None:
        files[_key(REGISTRY_PATH)] = registry_text
    if build_box:
        files[_key(BUILD_BOX_PATH)] = "fixture marker\n"
    if no_gpu:
        files[_key(NO_GPU_PATH)] = "fixture marker\n"
    return ReadOnlyHost(
        files=files,
        directories=directories,
        run_rc=run_rc,
        run_stdout=run_stdout,
        run_stderr=run_stderr,
        run_error=run_error,
    )


class ReportCase(unittest.TestCase):
    """Every command case proves the fake Host remained read-only."""

    def setUp(self) -> None:
        self.hosts: list[ReadOnlyHost] = []

    def tearDown(self) -> None:
        expected = _reader_argv()
        for host in self.hosts:
            self.assertFalse(host.write_attempted)
            for argv, _stdin in host.calls:
                self.assertEqual(argv, expected)

    def _host(
        self,
        *,
        build_box: bool = False,
        no_gpu: bool = False,
        registry_text: str | None = None,
        directories: Sequence[str] = (),
        run_rc: int = 0,
        run_stdout: str = "",
        run_stderr: str = "",
        run_error: BaseException | None = None,
    ) -> ReadOnlyHost:
        host = _base_host(
            build_box=build_box,
            no_gpu=no_gpu,
            registry_text=registry_text,
            directories=directories,
            run_rc=run_rc,
            run_stdout=run_stdout,
            run_stderr=run_stderr,
            run_error=run_error,
        )
        self.hosts.append(host)
        return host

    def _cli(self, host: ReadOnlyHost, argv: Sequence[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(proposals, "RealHost", return_value=host),
            patch.object(proposals, "SECTIONS", TRIGGERS_ONLY),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _registry(self, host: ReadOnlyHost) -> triggers.TriggerRegistry:
        loaded = triggers.load_trigger_registry(REGISTRY_PATH, host=host)
        self.assertFalse(loaded.errors)
        assert loaded.registry is not None
        return loaded.registry

    def test_committed_registry_runs_through_cli_on_the_build_box(self) -> None:
        host = self._host(build_box=True)
        registry = self._registry(host)
        code, stdout, stderr = self._cli(host, ["proposals"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        figures = {
            "eval-set": {"judged_queries": (0,), "held_out_ids": (0,)},
        }
        verdicts = tuple(
            watch.evaluate_trigger(
                trigger,
                {} if trigger.measure == "unavailable" else figures[trigger.measure],
            )
            for trigger in registry.watching
        )
        fired = sum(verdict.state == "fired" for verdict in verdicts)
        counts = {
            state: sum(trigger.state == state for trigger in registry.triggers)
            for state in triggers.STATES
        }
        self.assertIn(
            f"section triggers (product): {counts['watching']} watching, "
            f"{counts['acted']} acted, {counts['retired']} retired",
            stdout,
        )
        for verdict in verdicts:
            self.assertIn(f"  {verdict.trigger.id}: {verdict.state}", stdout)
            for clause_verdict in verdict.clauses:
                self.assertIn(clause_verdict.clause.figure, stdout)
            self.assertNotIn(verdict.trigger.says, stdout)
        self.assertEqual(
            stdout.count("\n  "),
            len(registry.watching),
        )
        self.assertTrue(
            stdout.rstrip().endswith(
                f"proposals: {fired} fired, {len(TRIGGERS_ONLY)} sections, 0 skipped"
            )
        )

    def test_product_section_is_skipped_without_the_build_box_marker(self) -> None:
        for both_markers in (False, True):
            with self.subTest(both_markers=both_markers):
                host = self._host(no_gpu=both_markers)
                if both_markers:
                    host.files[_key(BUILD_BOX_PATH)] = "fixture marker\n"
                    host.directories.add(_key(BUILD_BOX_PATH.parent))
                code, stdout, stderr = self._cli(host, ["proposals"])
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertIn(
                    "section triggers (product): skipped — "
                    "not the build box: host provision --build-box declares one",
                    stdout,
                )
                registry = self._registry(host)
                for trigger in registry.watching:
                    self.assertNotIn(f"  {trigger.id}:", stdout)
                self.assertTrue(
                    stdout.rstrip().endswith(
                        f"proposals: 0 fired, {len(TRIGGERS_ONLY)} sections, 1 skipped"
                    )
                )

    def test_malformed_registry_prints_all_findings_then_one_refusal(self) -> None:
        host = self._host(registry_text="version: 1\ntriggers: []\n")
        loaded = triggers.load_trigger_registry(REGISTRY_PATH, host=host)
        self.assertTrue(loaded.errors)
        code, stdout, stderr = self._cli(host, ["proposals"])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        lines = stderr.splitlines()
        self.assertEqual(
            lines[:-1], triggers.render_errors(loaded.errors).splitlines()
        )
        self.assertEqual(
            lines[-1],
            report.refusal(
                "proposals",
                "trigger registry could not be loaded",
                loaded.errors[0].fix,
            ),
        )
        self.assertEqual(len(lines), len(loaded.errors) + 1)

    def test_order_gate_refusal_row_and_walk_continuation(self) -> None:
        host = self._host(build_box=True)
        order: list[str] = []
        sections = (
            FakeSection(
                "office-first",
                "office",
                order,
                SectionReport("first detail", (Row("first-row", "fired", "1"),)),
            ),
            FakeSection(
                "product-refuse",
                "product",
                order,
                Problem("fictional read failed", "Fix the fictional reader."),
            ),
            FakeSection(
                "office-last",
                "office",
                order,
                SectionReport("last detail", (Row("last-row", "not fired", "0"),)),
            ),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = proposals.run_proposals(
                argparse.Namespace(), host=host, checkout_root=ROOT, sections=sections
            )
        rendered = output.getvalue()
        self.assertEqual(code, 1)
        self.assertEqual(order, ["office-first", "product-refuse", "office-last"])
        self.assertLess(
            rendered.index("section office-first"),
            rendered.index("section product-refuse"),
        )
        self.assertLess(
            rendered.index("section product-refuse"),
            rendered.index("section office-last"),
        )
        self.assertIn(
            "  product-refuse: refuse — fictional read failed Fix: Fix the fictional reader.",
            rendered,
        )
        self.assertIn("  last-row: not fired — 0", rendered)
        self.assertTrue(rendered.rstrip().endswith("proposals: 1 fired, 3 sections, 0 skipped"))

    def test_off_the_build_box_a_product_section_is_never_rendered(self) -> None:
        host = self._host()
        order: list[str] = []
        sections = (
            FakeSection(
                "product-first",
                "product",
                order,
                SectionReport("never printed", (Row("hidden-row", "fired", "1"),)),
            ),
            FakeSection(
                "office-last",
                "office",
                order,
                SectionReport("last detail", (Row("last-row", "not fired", "0"),)),
            ),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = proposals.run_proposals(
                argparse.Namespace(), host=host, checkout_root=ROOT, sections=sections
            )
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(order, ["office-last"])
        self.assertIn("section product-first (product): skipped — ", rendered)
        self.assertNotIn("hidden-row", rendered)
        self.assertIn("section office-last (office): last detail", rendered)
        self.assertTrue(rendered.rstrip().endswith("proposals: 0 fired, 2 sections, 1 skipped"))

    def test_query_seam_binds_sql_and_hides_command_diagnostics(self) -> None:
        for failure in ("exit", "could-not-run"):
            with self.subTest(failure=failure):
                host = self._host(
                    build_box=True,
                    run_rc=7 if failure == "exit" else 0,
                    run_stderr=DIAGNOSTIC,
                    run_error=(FileNotFoundError(DIAGNOSTIC) if failure == "could-not-run" else None),
                )
                section = QuerySection()
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = proposals.run_proposals(
                        argparse.Namespace(),
                        host=host,
                        checkout_root=ROOT,
                        sections=(section,),
                    )

                self.assertEqual(code, 1)
                self.assertIsInstance(section.result, Problem)
                assert isinstance(section.result, Problem)
                self.assertNotIn(DIAGNOSTIC, section.result.problem)
                self.assertNotIn(DIAGNOSTIC, section.result.fix)
                self.assertNotIn(DIAGNOSTIC, stdout.getvalue())
                self.assertNotIn(DIAGNOSTIC, stderr.getvalue())
                self.assertEqual(len(host.calls), 1)
                argv, sql = host.calls[0]
                self.assertEqual(argv, _reader_argv())
                self.assertEqual(sql, QUERY)
                self.assertNotIn(QUERY, argv)

    def test_seeded_eval_set_fires_from_fifty_primary_judgments(self) -> None:
        trigger_text = """version: 1
triggers:
  - id: fictional-ready
    reopens: §99.9
    register: E99
    measure: eval-set
    state: watching
    condition:
      all:
        - figure: judged_queries
          op: at-least
          value: 50
        - figure: held_out_ids
          op: at-least
          value: 1
    says: This trigger and its thresholds are fictitious test data.
"""
        set_root = ROOT / evalset.SET_ROOT
        judgment_path = set_root / judgments.JUDGMENTS_PATH
        records = (
            judgments.Judgment(
                query_id=f"judgments-{index:03d}",
                source_id=f"fictional/source-{index}",
                sha256=f"{index:064x}",
                start=0,
                end=1,
                grade=2,
                grader="CHU-attorney-1",
                assessment="primary",
            )
            for index in range(50)
        )
        held_out = set_root / "slices" / "judgments-held-out" / "fixture.ids"
        host = self._host(
            build_box=True,
            registry_text=trigger_text,
            directories=(_key(held_out.parent),),
        )
        host.files[_key(judgment_path)] = "".join(
            judgments.serialize(record_value) for record_value in records
        )
        host.files[_key(held_out)] = "fictional-held-out\n"
        host.directories.add(_key(held_out.parent))

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = proposals.run_proposals(
                argparse.Namespace(), host=host, checkout_root=ROOT,
                sections=TRIGGERS_ONLY,
            )
        rendered = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("section triggers (product): 1 watching, 0 acted, 0 retired", rendered)
        self.assertIn("  fictional-ready: fired", rendered)
        self.assertIn("judged_queries 50 at-least 50", rendered)
        self.assertIn("held_out_ids 1 at-least 1", rendered)
        self.assertTrue(rendered.rstrip().endswith("proposals: 1 fired, 1 sections, 0 skipped"))

    def test_improvement_package_imports_only_allowed_top_level_modules(self) -> None:
        allowed = set(sys.stdlib_module_names) | {"yaml", "gideon"}
        new_modules = {
            IMPROVEMENT / "feedback.py",
            IMPROVEMENT / "owuifeedback.py",
            IMPROVEMENT / "snapshot.py",
            IMPROVEMENT / "owuisnapshot.py",
            IMPROVEMENT / "packet.py",
            IMPROVEMENT / "ratings.py",
            IMPROVEMENT / "trips.py",
        }
        self.assertTrue(new_modules.issubset(set(IMPROVEMENT.rglob("*.py"))))
        for path in sorted(IMPROVEMENT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = (node.module or "",)
                else:
                    continue
                for name in names:
                    self.assertIn(name.split(".", 1)[0], allowed, path)

    def test_feedback_section_follows_triggers_and_rated_is_not_fired(self) -> None:
        self.assertEqual(
            tuple(section.name for section in proposals.SECTIONS),
            ("triggers", "feedback", "guardrail"),
        )
        self.assertIn("rated", proposals.ROW_STATES)
        host = self._host(build_box=True)
        host.files[str(ROOT / "config/site.example.yaml")] = (ROOT / "config/site.example.yaml").read_text()
        host.files[str(secrets.secret_path("gideon_admin_api_key"))] = "fictional-admin-key"
        page = {"items": [{
            "id": "fictional-rating-id", "type": "rating",
            "data": {"rating": 1, "model_id": "fictional-rated-model"},
            "meta": {"chat_id": "fictional-chat", "message_id": "fictional-message"},
            "created_at": 2_000_000_000,
        }], "total": 1}

        class Client:
            def request(self, method: str, path: str, **kwargs: object) -> owui.Response:
                del kwargs
                calls.append((method, path))
                return owui.Response(200, page)

        calls: list[tuple[str, str]] = []
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = proposals.run_proposals(
                argparse.Namespace(), host=host, checkout_root=ROOT,
                site_path=ROOT / "config/site.example.yaml",
                client_factory=lambda **kwargs: cast(owui.Client, Client()),
            )
        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertLess(rendered.index("section triggers"), rendered.index("section feedback"))
        self.assertIn("fictional-rated-model: rated", rendered)
        trigger_lines = rendered.split("section feedback", 1)[0].splitlines()
        fired = sum(": fired —" in line for line in trigger_lines)
        self.assertTrue(rendered.rstrip().endswith(
            f"proposals: {fired} fired, {len(proposals.SECTIONS)} sections, 0 skipped"
        ))
        self.assertEqual(calls, [("GET", f"{FEEDBACK_LIST_ROUTE}?page=1")])
        self.assertFalse(host.write_attempted)

    def test_guardrail_section_follows_feedback_and_reuses_its_read(self) -> None:
        fixed_now = 4_000_000_000.0
        reading = FeedbackReading(
            (
                FeedbackRecord(
                    "down",
                    "fictional-chat-a",
                    "fictional-message-a",
                    "fictional-model-a",
                    int(fixed_now - 10),
                ),
            ),
            0,
        )
        trip_lines = "fictional-family|fictional-pattern-a|fictional-chat-a|2\n"
        host = self._host(run_stdout=trip_lines)

        class CountingFeedback:
            def __init__(self) -> None:
                self.calls = 0

            def read(self) -> FeedbackReading:
                self.calls += 1
                return reading

        class Source:
            def __init__(self, reader: CountingFeedback) -> None:
                self.read = reader.read

        fake = CountingFeedback()
        output = io.StringIO()
        with (
            patch.object(proposals, "RealHost", return_value=host),
            patch.object(proposals.owuifeedback, "source", return_value=Source(fake)),
            patch("gideon.improvement.proposals.time.time", return_value=fixed_now),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(["proposals"])

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertLess(rendered.index("section feedback"), rendered.index("section guardrail"))
        self.assertIn("fictional-pattern-a: rated — 2 trips, 1 rated down", rendered)
        self.assertIn("fictional-chat-a: rated — message fictional-message-a", rendered)
        self.assertTrue(rendered.rstrip().endswith("proposals: 0 fired, 3 sections, 1 skipped"))
        self.assertEqual(fake.calls, 1)
        self.assertEqual(len(host.calls), 1)
        self.assertIn("FROM guardrail_trips", host.calls[0][1] or "")
        self.assertFalse(host.write_attempted)

    def test_refused_feedback_read_is_one_refuse_and_exits_one_without_writes(self) -> None:
        host = self._host(build_box=True)
        host.files[str(ROOT / "config/site.example.yaml")] = (ROOT / "config/site.example.yaml").read_text()
        host.files[str(secrets.secret_path("gideon_admin_api_key"))] = "fictional-admin-key"

        class Client:
            def request(self, method: str, path: str, **kwargs: object) -> owui.Response:
                del method, path, kwargs
                return owui.Response(503, "FICTIONAL_BODY_SENTINEL")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = proposals.run_proposals(
                argparse.Namespace(), host=host, checkout_root=ROOT,
                site_path=ROOT / "config/site.example.yaml",
                client_factory=lambda **kwargs: cast(owui.Client, Client()),
            )
        rendered = output.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("section feedback (office): refused", rendered)
        self.assertIn("feedback: refuse — Open WebUI answered", rendered)
        self.assertEqual(rendered.count("feedback: refuse —"), 1)
        self.assertIn("Fix: Check Open WebUI availability", rendered)
        self.assertNotIn("FICTIONAL_BODY_SENTINEL", rendered)
        trigger_lines = rendered.split("section feedback", 1)[0].splitlines()
        fired = sum(": fired —" in line for line in trigger_lines)
        self.assertTrue(rendered.rstrip().endswith(
            f"proposals: {fired} fired, {len(proposals.SECTIONS)} sections, 0 skipped"
        ))
        self.assertFalse(host.write_attempted)

    def test_proposals_help_names_the_surface_section(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as ctx:
            cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("proposals", help_text)
        self.assertIn("read the improvement proposals", help_text)
        self.assertIn("read-only report", help_text)


class FakeSection:
    """A report section whose returned value and invocation order are visible."""

    def __init__(
        self,
        name: str,
        scope: Scope,
        order: list[str],
        result: SectionReport | Problem,
    ) -> None:
        self.name = name
        self.scope = scope
        self.order = order
        self.result = result

    def render(self, _context: Context) -> SectionReport | Problem:
        self.order.append(self.name)
        return self.result


class QuerySection:
    """One office section that captures the query seam's returned value."""

    name = "query-reader"
    scope: Scope = "office"

    def __init__(self) -> None:
        self.result: tuple[str, ...] | Problem | None = None

    def render(self, context: Context) -> SectionReport | Problem:
        result = context.query(QUERY)
        self.result = result
        if isinstance(result, Problem):
            return result
        return SectionReport("query complete", ())


if __name__ == "__main__":
    unittest.main()
