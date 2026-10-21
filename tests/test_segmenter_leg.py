"""The segmenter image leg's content-free wire and host boundary."""

import ast
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from test_opinion_text import FICTIONAL_OPINION, cipher_text

from gideon.evaluation import evalset, segmenter_leg
from gideon.evaluation.results import ImageAccess, RunContext
from gideon.host.report import Problem
from gideon.host.sysio import Host
from gideon.worker import anchors, opiniontext, sections

SENTINEL = "Fictional opinion paragraph that must not appear in output"


def _reply(case_id: str = "segmenter-001") -> str:
    return json.dumps({
        "id": case_id,
        "sha256": "a" * 64,
        "length": 4,
        "sections": [[0, 4, "majority", "row"]],
        "markers": [],
        "pgmap_checked": 0,
        "pgmap_disagreeing": 0,
    }) + "\n"


class FakeHost:
    def __init__(self, *, stdout: str | None = None, rc: int = 0, timeout: bool = False) -> None:
        self.stdout = _reply() if stdout is None else stdout
        self.rc = rc
        self.timeout = timeout
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []
        self.remove_rc = 0

    def run(self, argv: list[str], *, input: str | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), input, timeout))
        if argv[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(argv, self.remove_rc, "", "")
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout or 1)
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, "private diagnostic")


def _context(host: FakeHost, *, image: bool = True) -> RunContext:
    return RunContext(
        cast(Host, host), "/tmp/segmenter-test-rendered", "/tmp/segmenter-test-rendered",
        None, None, 1, lambda _line: None,
        image=ImageAccess("registry.example/gideon@sha256:" + "e" * 64, "/tmp/segmenter-checkout") if image else None,
    )


class Entry(unittest.TestCase):
    """The image entry re-runs the worker's walk and writes only facts."""

    def test_columns_round_trip_and_leading_blank(self) -> None:
        raws = {
            "plain_text": "\n\n" + SENTINEL,
            "xml_harvard": (
                f'<root><p pgmap="1 2">{SENTINEL}<page-number>*2</page-number>After</p>'
                '<p pgmap="3 4">Fictional ending</p></root>'
            ),
            "html_columbia": f"<html><body><p>{SENTINEL}</p></body></html>",
            "html_lawbox": f"<html><body><p>{SENTINEL}</p></body></html>",
            "html_anon_2020": f"<html><body><p>{SENTINEL}</p></body></html>",
            "html": f"<html><body><p>{SENTINEL}</p></body></html>",
        }
        self.assertEqual(set(raws), set(opiniontext.TEXT_SOURCES))
        for column, raw in raws.items():
            with self.subTest(column=column):
                request = json.dumps({
                    "id": "segmenter-001", "column": column,
                    "opinion_type": "010combined", "per_curiam": False,
                    "original": raw,
                }) + "\n"
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("sys.stdin", io.StringIO(request)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = segmenter_leg.main()
                self.assertEqual((code, stderr.getvalue()), (0, ""))
                self.assertNotIn(SENTINEL, stdout.getvalue())
                answer = json.loads(stdout.getvalue())
                parsed = opiniontext.walk(column, raw)
                self.assertEqual(answer["sha256"], hashlib.sha256(parsed.text.encode("utf-8")).hexdigest())
                self.assertEqual(answer["length"], len(parsed.text))
                self.assertEqual(answer["sections"][0][0], 0)
                self.assertEqual(tuple(answer), (
                    "id", "sha256", "length", "sections", "markers",
                    "pgmap_checked", "pgmap_disagreeing",
                ))
                segmented = sections.segment(
                    parsed, opinion_type="010combined", per_curiam=False, column=column,
                )
                anchored = anchors.anchor(parsed, segmented)
                self.assertEqual(
                    (answer["pgmap_checked"], answer["pgmap_disagreeing"]),
                    (anchored.pgmap_checked, anchored.pgmap_disagreeing),
                )
                if column == "xml_harvard":
                    self.assertGreater(anchored.pgmap_checked, 0)
                    self.assertGreater(anchored.pgmap_disagreeing, 0)
                case = segmenter_leg.LegInput("segmenter-001", column, "010combined", False)
                result = segmenter_leg.run_leg(
                    (case,), {case.case_id: raw}, _context(FakeHost(stdout=stdout.getvalue())),
                )
                self.assertNotIsInstance(result, Problem)
                assert not isinstance(result, Problem)
                self.assertEqual(
                    (result[case.case_id].pgmap_checked, result[case.case_id].pgmap_disagreeing),
                    (anchored.pgmap_checked, anchored.pgmap_disagreeing),
                )

    def test_cipher_original_returns_unknown_sections(self) -> None:
        cipher = cipher_text(FICTIONAL_OPINION)
        request = json.dumps({
            "id": "segmenter-001", "column": "plain_text",
            "opinion_type": "010combined", "per_curiam": False,
            "original": cipher,
        }) + "\n"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(request)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = segmenter_leg.main()
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        parsed = opiniontext.walk("plain_text", cipher)
        self.assertEqual(json.loads(stdout.getvalue()), {
            "id": "segmenter-001",
            "sha256": hashlib.sha256(parsed.text.encode()).hexdigest(),
            "length": len(parsed.text),
            "sections": [[0, len(parsed.text), "unknown", "none"]],
            "markers": [],
            "pgmap_checked": 0,
            "pgmap_disagreeing": 0,
        })
        self.assertNotIn(cipher, stdout.getvalue())

    def test_malformed_input_refuses_without_echoing_opinion(self) -> None:
        requests = (
            "{not-json}\n",
            "null\n",
            json.dumps({"id": "segmenter-001", "column": "bad", "opinion_type": "lead", "per_curiam": False, "original": SENTINEL}) + "\n",
            json.dumps({"id": "segmenter-001", "column": "plain_text", "opinion_type": "lead", "per_curiam": False, "original": "   "}) + "\n",
        )
        for request in requests:
            with self.subTest(request=request[:10]):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("sys.stdin", io.StringIO(request)), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = segmenter_leg.main()
                self.assertNotEqual(code, 0)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("segmenter leg:", stderr.getvalue())
                self.assertNotIn(SENTINEL, stderr.getvalue())

    def test_worker_imports_live_inside_entry_function(self) -> None:
        tree = ast.parse(Path(segmenter_leg.__file__).read_text(encoding="utf-8"))
        top_level = (node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom)))
        self.assertFalse(any(
            "gideon.worker" in ast.unparse(node) for node in top_level
        ))

    def test_entry_runs_where_the_host_yaml_loader_is_absent(self) -> None:
        # The service image has no YAML loader; a module-level import that reached
        # one exited the container before it read a line.
        script = (
            "import runpy, sys\n"
            "sys.modules['yaml'] = None\n"
            "runpy.run_module('gideon.evaluation.segmenter_leg', run_name='__main__')\n"
        )
        request = json.dumps({
            "id": "segmenter-001", "column": "plain_text", "opinion_type": "010combined",
            "per_curiam": False, "original": SENTINEL,
        }) + "\n"
        result = subprocess.run(
            [sys.executable, "-c", script], input=request, capture_output=True, text=True,
            check=False, cwd=Path(segmenter_leg.__file__).resolve().parents[2],
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(json.loads(result.stdout)["id"], "segmenter-001")

    def test_case_id_rule_is_the_evaluation_sets(self) -> None:
        self.assertEqual(segmenter_leg.CASE_ID_PATTERN.pattern, evalset.SEGMENTER_ID_PATTERN.pattern)


class HostSide(unittest.TestCase):
    """The host sends originals on stdin through one isolated container."""

    def _assert_invalid_reply(self, value: dict[str, object]) -> None:
        host = FakeHost(stdout=json.dumps(value) + "\n")
        case = segmenter_leg.LegInput("segmenter-001", "plain_text", "lead", False)
        result = segmenter_leg.run_leg((case,), {case.case_id: SENTINEL}, _context(host))
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertIn("invalid reply line 1", result.problem)
        self.assertIn("tools.imagebuild gideon", result.fix)
        self.assertNotIn(SENTINEL, result.problem + result.fix)

    def test_reply_missing_pgmap_counts_refuses(self) -> None:
        for missing in (("pgmap_checked",), ("pgmap_disagreeing",), ("pgmap_checked", "pgmap_disagreeing")):
            with self.subTest(missing=missing):
                value = json.loads(_reply())
                for key in missing:
                    del value[key]
                self._assert_invalid_reply(value)

    def test_non_integer_or_negative_pgmap_counts_refuse(self) -> None:
        for key in ("pgmap_checked", "pgmap_disagreeing"):
            for invalid in (True, False, 1.0, "1", None, -1):
                with self.subTest(key=key, invalid=invalid):
                    value = json.loads(_reply())
                    value[key] = invalid
                    self._assert_invalid_reply(value)

    def test_pgmap_disagreeing_above_checked_refuses(self) -> None:
        value = json.loads(_reply())
        value["pgmap_disagreeing"] = value["pgmap_checked"] + 1
        self._assert_invalid_reply(value)

    def test_argv_stdin_and_reply(self) -> None:
        host = FakeHost()
        case = segmenter_leg.LegInput("segmenter-001", "plain_text", "lead", True)
        result = segmenter_leg.run_leg((case,), {case.case_id: SENTINEL}, _context(host))
        self.assertNotIsInstance(result, Problem)
        assert not isinstance(result, Problem)
        self.assertEqual(result[case.case_id].sections[0].section_type, "majority")
        argv, request, timeout = host.calls[0]
        self.assertEqual(argv[:3], ("docker", "run", "--rm"))
        self.assertIn("-i", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--log-driver") + 1], "none")
        self.assertTrue(argv[argv.index("--name") + 1].startswith(segmenter_leg.CONTAINER_PREFIX))
        self.assertTrue(argv[argv.index("-v") + 1].endswith(":ro"))
        self.assertEqual(argv[-2:], ("-m", "gideon.evaluation.segmenter_leg"))
        self.assertEqual(timeout, segmenter_leg.IMAGE_TIMEOUT_SECONDS)
        assert request is not None
        self.assertEqual(json.loads(request), {
            "id": case.case_id, "column": case.column, "opinion_type": case.opinion_type,
            "per_curiam": True, "original": SENTINEL,
        })
        self.assertNotIn(SENTINEL, str(result))

    def test_every_failed_container_reply_is_one_problem_with_image_fix(self) -> None:
        case = segmenter_leg.LegInput("segmenter-001", "plain_text", "lead", False)
        scenarios = (
            FakeHost(rc=2), FakeHost(stdout="not-json\n"), FakeHost(stdout=""),
            FakeHost(stdout=_reply("segmenter-002")), FakeHost(timeout=True),
        )
        for host in scenarios:
            with self.subTest(host=host):
                result = segmenter_leg.run_leg((case,), {case.case_id: SENTINEL}, _context(host))
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("tools.imagebuild gideon", result.fix)
                self.assertNotIn(SENTINEL, result.problem + result.fix)
                if host.timeout:
                    self.assertEqual(host.calls[1][0][:3], ("docker", "rm", "-f"))
                    self.assertEqual(host.calls[1][0][-1], host.calls[0][0][host.calls[0][0].index("--name") + 1])

    def test_no_image_access_does_not_launch_container(self) -> None:
        host = FakeHost()
        case = segmenter_leg.LegInput("segmenter-001", "plain_text", "lead", False)
        result = segmenter_leg.run_leg((case,), {case.case_id: SENTINEL}, _context(host, image=False))
        self.assertIsInstance(result, Problem)
        self.assertEqual(host.calls, [])

    def test_missing_original_refuses_before_launch(self) -> None:
        host = FakeHost()
        case = segmenter_leg.LegInput("segmenter-001", "plain_text", "lead", False)
        result = segmenter_leg.run_leg((case,), {}, _context(host))
        self.assertIsInstance(result, Problem)
        self.assertEqual(host.calls, [])
