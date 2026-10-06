"""The extraction runner's image protocol, scoring, and failure boundary."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import patch

from gideon.casecite import __main__
from gideon.evaluation.evalset import SET_ROOT, Case, LoadedSet, load_set, select_cases
from gideon.evaluation.extraction_slice import (
    CONTAINER_PREFIX,
    IMAGE_TIMEOUT_SECONDS,
    PROBLEMS,
    run_extraction,
)
from gideon.evaluation.results import ImageAccess, RunContext
from gideon.extraction import ExactObject
from gideon.extraction.contract import object_to_wire
from gideon.extraction.grammar import extract
from gideon.host.render.api import API_MOUNT_TARGET, API_WORKING_DIRECTORY
from gideon.host.sysio import Host, PathLike

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = "registry.example/gideon@sha256:" + "a" * 64
CHECKOUT = Path("/fictitious-checkout")


def _cases(*, full: bool = False) -> tuple[LoadedSet, tuple[str, ...]]:
    loaded = load_set(ROOT / SET_ROOT).loaded
    assert loaded is not None
    active = select_cases(loaded, "extraction").counted
    if full:
        return loaded, active

    def types(case: Case) -> set[str]:
        expected = cast(dict[str, object], case["expected"])
        labels = cast(list[dict[str, object]], expected["objects"])
        return {cast(str, label["type"]) for label in labels}

    cite_only = next(
        case_id for case_id in active if types(loaded.cases_by_id[case_id]) == {"case_cite"}
    )
    cite_and_statute = next(
        case_id for case_id in active
        if {"case_cite", "statute"} <= types(loaded.cases_by_id[case_id])
    )
    ids = (cite_only, cite_and_statute)
    return replace(loaded, slices={**loaded.slices, "extraction": ids}), ids


def _cites(case: Case) -> list[dict[str, object]]:
    expected = cast(dict[str, object], case["expected"])
    labels = cast(list[dict[str, object]], expected["objects"])
    return [
        object_to_wire(ExactObject(
            "case_cite", cast(int, label["start"]), cast(int, label["end"]),
            cast(str, label["text"]),
        ))
        for label in labels if label["type"] == "case_cite"
    ]


def _reply(input_text: str, loaded: LoadedSet) -> str:
    return "".join(
        json.dumps({"id": request["id"], "objects": _cites(loaded.cases_by_id[request["id"]])}) + "\n"
        for request in (json.loads(line) for line in input_text.splitlines())
    )


class ImageHost:
    """Record a single image command and return a chosen text response."""

    def __init__(self, response: Callable[[str], str], *, returncode: int = 0, error: str = "") -> None:
        self.response = response
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, passthrough
        self.calls.append((tuple(argv), input, timeout))
        if tuple(argv)[:2] == ("docker", "rm"):
            return subprocess.CompletedProcess(list(argv), 0, "", "")
        if self.error == "timeout":
            assert timeout is not None
            raise subprocess.TimeoutExpired(argv, timeout)
        if self.error == "cannot-run":
            raise OSError("PRIVATE MATTER TEXT")
        assert input is not None
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.response(input), "PRIVATE MATTER TEXT"
        )


def _context(host: ImageHost, *, image: bool = True) -> RunContext:
    return RunContext(
        host=cast(Host, host),
        turns_dir="/fictitious-turns",
        production_dir="/fictitious-rendered",
        served_model_name=None,
        judge_prompt_id=None,
        repeats=1,
        progress=lambda _line: None,
        image=ImageAccess(REFERENCE, CHECKOUT) if image else None,
    )


class ImageLeg(unittest.TestCase):
    def test_one_run_sends_counted_questions_in_the_entry_shape_and_gates_citations(self) -> None:
        loaded, ids = _cases(full=True)
        host = ImageHost(lambda input_text: _reply(input_text, loaded))
        result = run_extraction(loaded, "extraction", _context(host))
        self.assertEqual(len(host.calls), 1)
        argv, input_text, timeout = host.calls[0]
        self.assertEqual(timeout, IMAGE_TIMEOUT_SECONDS)
        name = argv[5]
        self.assertTrue(name.startswith(CONTAINER_PREFIX))
        self.assertEqual(
            argv,
            (
                "docker", "run", "--rm", "-i", "--name", name, "--network", "none",
                "--log-driver", "none", "-v",
                f"{CHECKOUT / 'gideon'}:{API_MOUNT_TARGET}:ro",
                "-w", API_WORKING_DIRECTORY, REFERENCE,
                "python", "-m", "gideon.casecite",
            ),
        )
        assert input_text is not None
        requests = [json.loads(line) for line in input_text.splitlines()]
        self.assertEqual(requests, [
            {"id": case_id, "text": loaded.cases_by_id[case_id]["question"]}
            for case_id in ids
        ])
        self.assertEqual(input_text, "".join(json.dumps(request) + "\n" for request in requests))

        fake_adapter = ModuleType("gideon.casecite.adapter")
        fake_adapter.extract_case_cites = lambda _text: ()  # type: ignore[attr-defined]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(sys.modules, {"gideon.casecite.adapter": fake_adapter}),
            patch.object(sys, "stdin", io.StringIO(input_text)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(__main__.main(), 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            [json.loads(line)["id"] for line in stdout.getvalue().splitlines()], list(ids)
        )
        self.assertTrue(result.verdict, result.report)
        self.assertTrue(all(row.verdict == "pass" for row in result.results))
        self.assertIn("case_cite", result.report)
        self.assertIn("image leg elapsed ", result.report)
        self.assertNotIn("unlanded case_cite", result.report)

    def test_grammar_wins_an_overlap_from_the_image(self) -> None:
        loaded, ids = _cases()
        statute_case = loaded.cases_by_id[ids[1]]
        grammar = extract(cast(str, statute_case["question"]))
        statute = next(obj for obj in grammar if obj.type == "statute")
        overlap = object_to_wire(ExactObject(
            "case_cite", statute.start, statute.end, statute.text
        ))

        def response(input_text: str) -> str:
            requests = [json.loads(line) for line in input_text.splitlines()]
            return "".join(
                json.dumps({
                    "id": request["id"],
                    "objects": _cites(loaded.cases_by_id[request["id"]])
                    + ([overlap] if request["id"] == ids[1] else []),
                }) + "\n"
                for request in requests
            )

        result = run_extraction(loaded, "extraction", _context(ImageHost(response)))
        self.assertIn("case_cite 2 0 0", result.report)
        self.assertNotIn(f"false hit {ids[1]} case_cite {statute.start}:{statute.end}", result.report)

    def test_every_operational_failure_fails_every_counted_case_without_text(self) -> None:
        loaded, ids = _cases()
        valid = _reply(
            "".join(json.dumps({"id": case_id, "text": loaded.cases_by_id[case_id]["question"]}) + "\n" for case_id in ids),
            loaded,
        )
        wrong_span = json.loads(valid.splitlines()[0])
        wrong_span["objects"][0]["text"] = "Z" * len(wrong_span["objects"][0]["text"])
        outside_type = json.loads(valid.splitlines()[0])
        outside_type["objects"] = [object_to_wire(
            next(obj for obj in extract(cast(str, loaded.cases_by_id[ids[1]]["question"])) if obj.type == "statute")
        )]
        variants = (
            ("nonzero", ImageHost(lambda _input: "PRIVATE MATTER TEXT", returncode=3)),
            ("timeout", ImageHost(lambda _input: "", error="timeout")),
            ("cannot-run", ImageHost(lambda _input: "", error="cannot-run")),
            ("unreadable", ImageHost(lambda _input: "PRIVATE MATTER TEXT\n")),
            ("unknown-id", ImageHost(lambda _input: json.dumps({"id": "unknown", "objects": []}) + "\n")),
            ("duplicate-id", ImageHost(lambda _input: valid.splitlines()[0] + "\n" + valid)),
            ("missing-reply", ImageHost(lambda _input: "")),
            ("invalid-shape", ImageHost(lambda _input: json.dumps({"id": ids[0], "objects": [()]}) + "\n")),
            ("wrong-span", ImageHost(lambda _input: json.dumps(wrong_span) + "\n" + valid.splitlines()[1] + "\n")),
            ("outside-type", ImageHost(lambda _input: json.dumps(outside_type) + "\n" + valid.splitlines()[1] + "\n")),
        )
        for name, host in variants:
            with self.subTest(name=name):
                result = run_extraction(loaded, "extraction", _context(host))
                self.assertFalse(result.verdict)
                self.assertEqual({row.case_id for row in result.results}, set(ids))
                self.assertTrue(all(row.verdict == "fail" for row in result.results))
                self.assertTrue(all(row.metrics == {"problem": "image-leg-failed"} for row in result.results))
                self.assertEqual(PROBLEMS, frozenset({"image-leg-failed"}))
                self.assertIn("Fix: Run sudo python3 -m tools.imagebuild gideon --to registry.example --check", result.report)
                self.assertIn("image leg elapsed ", result.report)
                self.assertNotIn("PRIVATE MATTER TEXT", result.report)
                for case_id in ids:
                    self.assertNotIn(cast(str, loaded.cases_by_id[case_id]["question"]), result.report)
                removals = [argv for argv, _input, _timeout in host.calls if argv[:2] == ("docker", "rm")]
                if name == "timeout":
                    run_name = host.calls[0][0][5]
                    self.assertEqual(removals, [("docker", "rm", "-f", run_name)])
                else:
                    self.assertEqual(removals, [])

    def test_without_image_access_uses_grammar_alone_and_makes_no_host_call(self) -> None:
        loaded, _ids = _cases()
        host = ImageHost(lambda _input: "")
        result = run_extraction(loaded, "extraction", _context(host, image=False))
        self.assertEqual(host.calls, [])
        self.assertIn("unlanded case_cite", result.report)
        self.assertNotIn("image leg elapsed", result.report)
