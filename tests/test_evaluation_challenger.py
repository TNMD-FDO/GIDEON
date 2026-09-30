"""The committed challenger loader and its registered subject."""

import copy
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]

from gideon.evaluation import challenger, judge
from gideon.evaluation.results import RunContext
from gideon.host.sysio import Host

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / challenger.CHALLENGER_PATH
SENTINEL = "visibly-fictitious-secret-value"


class DictHost:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error

    def read_text(self, _path: object, *, encoding: str = "utf-8") -> str:
        if self.error is not None:
            raise self.error
        return self.text


def _document() -> dict[str, Any]:
    document = yaml.safe_load(PATH.read_text())
    assert isinstance(document, dict)
    return copy.deepcopy(document)


def _load(document: dict[str, Any]) -> challenger.ChallengerLoadResult:
    return challenger.load_challenger(PATH, host=cast(Host, DictHost(yaml.safe_dump(document))))


class Loader(unittest.TestCase):
    def test_committed_and_none_set(self) -> None:
        committed = challenger.load_challenger(PATH)
        self.assertTrue(committed.ok)
        document = _document()
        document["challenger"] = None
        unset = _load(document)
        self.assertTrue(unset.ok)
        assert unset.config is not None
        self.assertIsNone(unset.config.challenger)

    def test_shape_findings_accumulate_without_echoing_values(self) -> None:
        document = _document()
        entry = document["challenger"]
        document["versoin"] = SENTINEL
        document["version"] = challenger.SCHEMA_VERSION + 1
        entry["namme"] = SENTINEL
        entry["name"] = "Bad_Name"
        entry["subject"] = "judge-promt"
        entry.pop("release")
        entry["challenger"] = 7
        entry["set"] = "2026-99-99 fictitious"
        result = _load(document)
        self.assertFalse(result.ok)
        paths = {finding.key_path for finding in result.findings}
        self.assertEqual(paths, {
            "versoin", "version", "challenger.namme", "challenger.name",
            "challenger.subject", "challenger.release", "challenger.challenger",
            "challenger.set",
        })
        rendered = challenger.render_findings(result.findings)
        self.assertIn("nearest valid key is 'version'", rendered)
        self.assertIn("nearest valid key is 'name'", rendered)
        self.assertIn("nearest valid subject is 'judge-prompt'", rendered)
        self.assertNotIn(SENTINEL, rendered)
        self.assertTrue(all(finding.fix for finding in result.findings))

    def test_non_mapping_entry_and_missing_root_keys(self) -> None:
        for document, expected in (
            ({"version": challenger.SCHEMA_VERSION, "challenger": SENTINEL}, "challenger"),
            ({"version": challenger.SCHEMA_VERSION}, "challenger"),
            ({"challenger": None}, "version"),
        ):
            with self.subTest(document=document):
                result = _load(document)
                self.assertIn(expected, {finding.key_path for finding in result.findings})
                self.assertNotIn(SENTINEL, challenger.render_findings(result.findings))

    def test_host_and_yaml_failures(self) -> None:
        failures: tuple[tuple[str, Exception | None, str], ...] = (
            ("", FileNotFoundError(), "missing"),
            ("", PermissionError(), "unreadable"),
            ("", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), "UTF-8"),
            ("", None, "empty"),
            ("[]", None, "mapping"),
            ("challenger: [", None, "parse"),
            ("version: 1\nversion: 1\n", None, "duplicate"),
        )
        for text, error, reason in failures:
            with self.subTest(reason=reason):
                result = challenger.load_challenger(PATH, host=cast(Host, DictHost(text, error)))
                self.assertFalse(result.ok)
                self.assertEqual(len(result.findings), 1)
                self.assertIn(reason, result.findings[0].problem)
                self.assertTrue(result.findings[0].fix)


class Subject(unittest.TestCase):
    def test_judge_prompt_checks(self) -> None:
        loaded = challenger.load_challenger(PATH)
        assert loaded.config is not None and loaded.config.challenger is not None
        entry = loaded.config.challenger
        subject = next(item for item in challenger.SUBJECTS if item.name == entry.subject)
        unknown = "visibly-fictitious-prompt"
        cases = (
            (entry.release, unknown, "registered judge prompt"),
            (entry.release, entry.release, "must differ"),
            (entry.challenger, entry.release, "slice's registered judge prompt"),
        )
        for release, candidate, reason in cases:
            with self.subTest(reason=reason):
                findings = subject.check(subject.slice_name, release, candidate)
                self.assertTrue(any(reason in finding.problem for finding in findings))
                self.assertTrue(all(finding.fix for finding in findings))
        changed = replace(judge.PROMPT_REGISTRY[entry.challenger], schema_name="fictitious-schema")
        with patch.dict(judge.PROMPT_REGISTRY, {entry.challenger: changed}):
            findings = subject.check(subject.slice_name, entry.release, entry.challenger)
        self.assertTrue(any("share slots and schema name" in finding.problem for finding in findings))

    def test_change_replaces_only_judge_prompt_and_vocabulary_is_closed(self) -> None:
        loaded = challenger.load_challenger(PATH)
        assert loaded.config is not None and loaded.config.challenger is not None
        entry = loaded.config.challenger
        subject = next(item for item in challenger.SUBJECTS if item.name == entry.subject)
        context = RunContext(
            host=cast(Host, DictHost()), rendered_dir="/tmp/turns", engine_dir="/tmp/engine",
            served_model_name="fixture-model", judge_prompt_id=entry.release,
            repeats=1, progress=lambda _line: None,
        )
        self.assertEqual(subject.change(context, entry.challenger), replace(context, judge_prompt_id=entry.challenger))
        self.assertEqual(subject.slice_name, "judge-triples")
        self.assertEqual(challenger.OVERRIDE_KEY, "challenger")
        self.assertEqual(
            (challenger.NAME_FIELD, challenger.SUBJECT_FIELD, challenger.SIDE_FIELD,
             challenger.VALUE_FIELD, challenger.PAIRS_FIELD),
            ("name", "subject", "side", "value", "pairs"),
        )
        self.assertEqual((challenger.RELEASE_SIDE, challenger.CHALLENGER_SIDE), ("release", "challenger"))


if __name__ == "__main__":
    unittest.main()
