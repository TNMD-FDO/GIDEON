"""The reference writer's ordered stages and host-seam effects."""

import ast
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from unittest.mock import patch

import gideon
from gideon.cli import main
from gideon.evaluation import reference, reference_command
from gideon.evaluation.evalset import SET_ROOT, load_set
from gideon.host.sysio import Command, PathLike
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]
# This case reads no excluded file; it names one because the boundary moves the slice's shape.
HARVEST_IDS_PATH = Path("eval/sets/eval-v1/slices/extraction/harvest.ids")
RESEARCH_QA_CASES_PATH = Path("eval/sets/eval-v1/research-qa/harvest.jsonl")
RUN_ID = "11111111-2222-4333-8444-555555555555"
GIT_SHA = "a" * 40


class WriterHost:
    """A fake Host backed by a temporary checkout and recorded command answers."""

    def __init__(self, document: Mapping[str, object], *, reader_rc: int = 0) -> None:
        self.document = document
        self.reader_rc = reader_rc
        self.tag_rc = 0
        self.tag_stdout = GIT_SHA + "\n"
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.writes: list[Path] = []
        self.chowns: list[tuple[Path, int, int]] = []

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
        if command[0] == "git":
            return subprocess.CompletedProcess(list(command), self.tag_rc, self.tag_stdout, "git diagnostic")
        if command[0] == "docker":
            return subprocess.CompletedProcess(
                list(command), self.reader_rc, json.dumps(self.document), "database diagnostic"
            )
        raise AssertionError(f"unexpected command: {command}")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        return Path(path).read_text(encoding=encoding)

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del mode
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding=encoding)
        self.writes.append(target)

    def exists(self, path: PathLike) -> bool:
        return Path(path).exists()

    def listdir(self, path: PathLike) -> list[str]:
        return os.listdir(path)

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        Path(path).unlink(missing_ok=missing_ok)

    def stat(self, path: PathLike) -> os.stat_result:
        return Path(path).stat()

    def chmod(self, path: PathLike, mode: int) -> None:
        Path(path).chmod(mode)

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        self.chowns.append((Path(path), uid, gid))

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        Path(path).mkdir(mode=mode, parents=parents, exist_ok=exist_ok)

    def geteuid(self) -> int:
        return 0


def _checkout(directory: str) -> Path:
    checkout = Path(directory) / "checkout"
    checkout.mkdir()
    shutil.copytree(ROOT / "eval", checkout / "eval")
    shutil.copy(ROOT / "courts.yaml", checkout / "courts.yaml")
    return checkout


def _loaded(checkout: Path):
    loaded = load_set(checkout / SET_ROOT).loaded
    assert loaded is not None
    return loaded


def _document(loaded, **overrides: object) -> dict[str, object]:
    result_verdicts = overrides.pop("result_verdicts", {})
    result_rows = overrides.pop("results", None)
    results = [
        {
            "case_id": case_id,
            "repeat": 1,
            "verdict": (
                result_verdicts.get(case_id, "pass")
                if isinstance(result_verdicts, Mapping)
                else "pass"
            ),
        }
        for case_id in loaded.slices["extraction"]
        if case_id in loaded.active_ids
    ]
    run: dict[str, object] = {
        "run_id": RUN_ID,
        "product_version": gideon.__version__,
        "corpus_lockfile": None,
        "eval_set_version": loaded.version,
        "hardware_profile": "fictitious-profile",
        "slice": "extraction",
        "overrides": {},
        "repeats": 1,
        "git_sha": GIT_SHA,
        "git_dirty": False,
        "set_digest": loaded.digest,
        "verdict": "pass",
    }
    run.update(overrides)
    return {"run": run, "results": results if result_rows is None else result_rows}


def _write_references(
    checkout: Path,
    loaded,
    cases: Mapping[str, reference.Verdict],
    *,
    eval_set_version: str | None = None,
) -> None:
    version = loaded.version if eval_set_version is None else eval_set_version
    for list_name, ids in loaded.slice_lists["extraction"].items():
        value = reference.ReferenceFile(
            format=reference.FORMAT_VERSION,
            product_version=gideon.__version__,
            corpus_lockfile=None,
            eval_set_version=version,
            hardware_profile="fictitious-profile",
            tag=f"v{gideon.__version__}",
            slice="extraction",
            list=list_name,
            repeats=1,
            set_digest=loaded.digest,
            cases={case_id: cases[case_id] for case_id in ids if case_id in cases},
        )
        path = checkout / reference.REFERENCE_ROOT / "extraction" / f"{list_name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reference.serialize_reference(value), encoding="utf-8")


def _invoke(host: WriterHost, checkout: Path, run_id: str = RUN_ID) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    real = reference_command.run_reference
    injected = lambda args: real(  # noqa: E731
        args,
        host=host,
        checkout_root=checkout,
        court_path=ROOT / "courts.yaml",
    )
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch.object(
        reference_command, "run_reference", side_effect=injected
    ):
        code = main(["eval", "reference", "--run", run_id])
    return code, stdout.getvalue(), stderr.getvalue()


class CheckRefusals(unittest.TestCase):
    """Every writer check stops before writing and prints an actionable fix."""

    def assert_check_refusal(self, **changes: object) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _checkout(directory)
            loaded = _loaded(checkout)
            host = WriterHost(_document(loaded, **changes))
            result = _invoke(host, checkout)
        self.assertEqual(result[0], 1)
        self.assertIn("check: refuse", result[1])
        self.assertIn("Fix:", result[1])
        self.assertNotIn("write: ok", result[1])
        return result

    def test_dirty_sha_overrides_unknown_and_suite_wide_slice_refuse(self) -> None:
        cases = (
            ({"git_dirty": True}, "clean checkout"),
            ({"git_sha": None}, "clean checkout"),
            ({"overrides": {"fictitious": "override"}}, "without overrides"),
            ({"slice": "missing-slice"}, "loaded eval set"),
            ({"slice": None}, "tickets 12 and 13"),
            ({"slice": "judge-triples"}, "compares against a reference"),
            ({"eval_set_version": "eval-v-fictitious-other"}, "release's eval set"),
            ({"set_digest": "e" * 64}, "release's eval set"),
        )
        for changes, fix in cases:
            with self.subTest(changes=changes):
                _code, stdout, _stderr = self.assert_check_refusal(**changes)
                self.assertIn(fix, stdout)

    def test_tag_that_does_not_name_the_run_sha_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _checkout(directory)
            loaded = _loaded(checkout)
            host = WriterHost(_document(loaded))
            host.tag_stdout = "b" * 40 + "\n"
            code, stdout, _stderr = _invoke(host, checkout)
        self.assertEqual(code, 1)
        self.assertIn("does not name the recorded git sha", stdout)
        self.assertIn("rev-parse --verify", stdout)

    def test_regression_names_ids_and_the_remove_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _checkout(directory)
            loaded = _loaded(checkout)
            target = next(case_id for case_id in loaded.active_ids if case_id in loaded.slices["extraction"])
            cases: dict[str, reference.Verdict] = dict.fromkeys(
                loaded.active_ids, cast(reference.Verdict, "pass")
            )
            _write_references(checkout, loaded, cases)
            host = WriterHost(_document(loaded, result_verdicts={target: "fail"}))
            code, stdout, _stderr = _invoke(host, checkout)
        self.assertEqual(code, 1)
        self.assertIn(target, stdout)
        self.assertIn("Repair the regression or supersede", stdout)
        self.assertIn("remove every reference file", stdout)
        self.assertFalse(host.writes)

    def test_unsigned_result_refuses_with_signoff_fix(self) -> None:
        if absent_from_export(RESEARCH_QA_CASES_PATH, ROOT):
            self.skipTest("in an export the only sign-off-taking cases are absent, so none is unsigned")
        with tempfile.TemporaryDirectory() as directory:
            checkout = _checkout(directory)
            loaded = _loaded(checkout)
            unsigned_id = next(iter(loaded.unsigned_ids))
            document = _document(
                loaded,
                results=(
                    {"case_id": unsigned_id, "repeat": 1, "verdict": "pass"},
                ),
            )
            host = WriterHost(document)
            code, stdout, _stderr = _invoke(host, checkout)
        self.assertEqual(code, 1)
        self.assertIn(unsigned_id, stdout)
        self.assertIn("sign the case through the sign-off kit", stdout.lower())
        self.assertIn("selects through the loader", stdout)
        self.assertFalse(host.writes)


class ReadRefusals(unittest.TestCase):
    """Reader failures stop before checks and preserve their problem/fix pair."""

    def test_no_such_run_and_unreachable_database_refuse(self) -> None:
        cases: tuple[tuple[int, Mapping[str, object], str], ...] = (
            (0, {"run": None, "results": []}, "no evaluation run exists"),
            (1, {"run": None, "results": []}, "sudo python3"),
        )
        for reader_rc, document, expected in cases:
            with self.subTest(reader_rc=reader_rc):
                with tempfile.TemporaryDirectory() as directory:
                    checkout = _checkout(directory)
                    host = WriterHost(document, reader_rc=reader_rc)
                    code, stdout, _stderr = _invoke(host, checkout)
                self.assertEqual(code, 1)
                self.assertIn("read: refuse", stdout)
                self.assertIn(expected, stdout)

    def test_non_uuid_is_refused_before_reader_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _checkout(directory)
            loaded = _loaded(checkout)
            host = WriterHost(_document(loaded))
            code, stdout, _stderr = _invoke(host, checkout, "not-a-uuid")
        self.assertEqual(code, 1)
        self.assertIn("not a UUID", stdout)
        self.assertFalse(any(argv[0] == "docker" for argv, _ in host.calls))


class Writing(unittest.TestCase):
    """Writing is partitioned, owned, canonical, and repeatable."""

    def test_malformed_reference_requires_removal_then_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _checkout(directory)
            loaded = _loaded(checkout)
            cases: dict[str, reference.Verdict] = dict.fromkeys(
                loaded.active_ids, cast(reference.Verdict, "pass")
            )
            _write_references(checkout, loaded, cases)
            paths = sorted((checkout / reference.REFERENCE_ROOT / "extraction").glob("*.json"))
            paths[0].write_text(paths[0].read_text(encoding="utf-8") + " ", encoding="utf-8")
            host = WriterHost(_document(loaded))
            code, stdout, stderr = _invoke(host, checkout)
            self.assertEqual(code, 1)
            self.assertIn(reference.SLICE_REPAIR_FIX, stdout)
            self.assertIn("canonical serialization", stderr)

            for path in paths:
                path.unlink()
            code, stdout, stderr = _invoke(host, checkout)
            self.assertEqual(code, 0)
            self.assertIn("written", stdout)
            self.assertEqual(stderr, "")

    def test_partial_reference_still_refuses_after_one_file_is_removed(self) -> None:
        if absent_from_export(HARVEST_IDS_PATH, ROOT):
            self.skipTest("in an export the slice has one id list and removing its file is absence")
        with tempfile.TemporaryDirectory() as directory:
            checkout = _checkout(directory)
            loaded = _loaded(checkout)
            self.assertGreater(
                len(loaded.slice_lists["extraction"]),
                1,
                "the loaded extraction slice has more than one id list",
            )
            host = WriterHost(_document(loaded))
            code, stdout, stderr = _invoke(host, checkout)
            self.assertEqual(code, 0)
            self.assertIn("written", stdout)
            self.assertEqual(stderr, "")

            paths = sorted((checkout / reference.REFERENCE_ROOT / "extraction").glob("*.json"))
            paths[0].unlink()
            code, stdout, stderr = _invoke(host, checkout)
        self.assertEqual(code, 1)
        self.assertIn("partial reference", stderr)
        self.assertIn(reference.SLICE_REPAIR_FIX, stdout)

    def test_writing_twice_is_byte_identical_and_second_run_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _checkout(directory)
            loaded = _loaded(checkout)
            host = WriterHost(_document(loaded))
            first = _invoke(host, checkout)
            files = sorted((checkout / reference.REFERENCE_ROOT / "extraction").glob("*.json"))
            before = {path: path.read_bytes() for path in files}
            second = _invoke(host, checkout)
            after = {path: path.read_bytes() for path in files}
            owner = checkout.stat()
        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], 0)
        self.assertIn("written", first[1])
        self.assertIn("unchanged", second[1])
        self.assertEqual(before, after)
        for path in files:
            self.assertIn((path, owner.st_uid, owner.st_gid), host.chowns)

    def test_clean_written_references_hold_no_unsigned_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = _checkout(directory)
            loaded = _loaded(checkout)
            host = WriterHost(_document(loaded))
            code, _stdout, _stderr = _invoke(host, checkout)
            files = sorted((checkout / reference.REFERENCE_ROOT / "extraction").glob("*.json"))
            contents = tuple(path.read_text(encoding="utf-8") for path in files)
        self.assertEqual(code, 0)
        self.assertTrue(files)
        for unsigned_id in loaded.unsigned_ids:
            self.assertTrue(all(unsigned_id not in content for content in contents))


class Imports(unittest.TestCase):
    """The new evaluation modules stay within the standard-library boundary."""

    def test_reference_modules_import_only_allowed_tops(self) -> None:
        allowed = set(sys.stdlib_module_names) | {"gideon", "yaml"}
        for path in (ROOT / "gideon/evaluation/reference.py", ROOT / "gideon/evaluation/reference_command.py"):
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


if __name__ == "__main__":
    unittest.main()
