"""Contracts for the committed trigger registry and its loader."""

from __future__ import annotations

import os
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any, cast

from gideon.host.sysio import Command, PathLike
from gideon.improvement import triggers
from gideon.improvement.triggers import (
    Clause,
    Condition,
    Trigger,
    TriggerError,
    TriggerRegistry,
    TriggerRegistryLoadResult,
    default_triggers_path,
    load_trigger_registry,
    render_errors,
    validate_trigger_registry,
)

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "config/triggers.yaml"
FAKE_PATH = "/tmp/gideon-trigger-registry.yaml"


class DictHost:
    """Dict-backed Host seam for trigger-registry read tests."""

    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        errors: Mapping[str, BaseException] | None = None,
    ) -> None:
        self.files = dict(files or {})
        self.errors = dict(errors or {})

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
        del check, input, cwd, env, timeout, passthrough
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        key = os.fspath(path)
        if key in self.errors:
            raise self.errors[key]
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
        del encoding, mode
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        del path
        raise NotImplementedError

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del path, missing_ok
        raise NotImplementedError

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode
        raise NotImplementedError

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid
        raise NotImplementedError

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok
        raise NotImplementedError

    def geteuid(self) -> int:
        return 0


def _entry(**overrides: object) -> dict[str, object]:
    """A valid watching entry; an override of None removes its key."""
    entry: dict[str, object] = {
        "id": "example-trigger",
        "reopens": "§99.9",
        "register": "E99",
        "measure": "eval-set",
        "state": "watching",
        "condition": {
            "figure": "judged_queries",
            "op": "at-least",
            "value": 1,
        },
        "says": "A fictitious trigger condition is recorded here.",
    }
    entry.update(overrides)
    return {key: value for key, value in entry.items() if value is not None}


def _document(*entries: dict[str, object], **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": 1,
        "triggers": list(entries) or [_entry()],
    }
    document.update(overrides)
    return document


def _entry_document(**overrides: object) -> dict[str, object]:
    return _document(_entry(**overrides))


class CommittedArtifact(unittest.TestCase):
    """The release registry is the contract for the first two triggers."""

    def test_committed_artifact_loads_with_the_ruled_entries(self) -> None:
        result = load_trigger_registry(ARTIFACT)

        self.assertTrue(result.ok, render_errors(result.errors))
        self.assertIsInstance(result.registry, TriggerRegistry)
        assert result.registry is not None
        self.assertEqual(result.registry.version, 1)
        self.assertEqual(
            [trigger.id for trigger in result.registry.triggers],
            ["judge-fallback", "learned-components"],
        )
        self.assertEqual(
            [
                (trigger.state, trigger.measure, trigger.register)
                for trigger in result.registry.triggers
            ],
            [
                ("watching", "unavailable", "M18"),
                ("watching", "eval-set", "E20"),
            ],
        )
        self.assertEqual(
            [trigger.id for trigger in result.registry.watching],
            [trigger.id for trigger in result.registry.triggers],
        )

    def test_committed_conditions_have_the_ruled_grammar(self) -> None:
        result = load_trigger_registry(ARTIFACT)
        assert result.registry is not None

        judge = result.registry.by_id["judge-fallback"]
        self.assertIsNotNone(judge.condition)
        assert judge.condition is not None
        self.assertEqual(len(judge.condition.clauses), 1)
        self.assertEqual(
            judge.condition.clauses[0],
            Clause("panel_disagreement_pct", "above", 20, runs=2),
        )

        learned = result.registry.by_id["learned-components"]
        self.assertIsNotNone(learned.condition)
        assert learned.condition is not None
        self.assertEqual(
            learned.condition.clauses,
            (
                Clause("judged_queries", "at-least", 50),
                Clause("held_out_ids", "at-least", 1),
            ),
        )

    def test_only_watching_entries_carry_a_condition(self) -> None:
        result = load_trigger_registry(ARTIFACT)
        assert result.registry is not None
        for trigger in result.registry.triggers:
            with self.subTest(trigger=trigger.id):
                self.assertEqual(
                    trigger.condition is not None, trigger.state == "watching"
                )
                self.assertEqual(trigger.ruled is None, trigger.state == "watching")

    def test_trigger_dataclasses_are_frozen_and_slotted(self) -> None:
        dataclasses = (
            Clause,
            Condition,
            Trigger,
            TriggerRegistry,
            TriggerError,
            TriggerRegistryLoadResult,
        )
        for dataclass in dataclasses:
            with self.subTest(dataclass=dataclass.__name__):
                self.assertTrue(is_dataclass(dataclass))
                parameters = cast(Any, dataclass).__dataclass_params__
                self.assertTrue(parameters.frozen)
                self.assertTrue(hasattr(dataclass, "__slots__"))

    def test_default_path_points_at_the_committed_artifact(self) -> None:
        self.assertEqual(default_triggers_path().resolve(), ARTIFACT.resolve())


class VocabularyDrift(unittest.TestCase):
    """The committed file uses only the loader's closed vocabularies."""

    def test_committed_file_keys_and_values_are_registered(self) -> None:
        result = load_trigger_registry(ARTIFACT)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.document is not None
        document = result.document

        self.assertTrue(set(document).issubset(triggers._ROOT_KEYS))
        entries = document["triggers"]
        self.assertIsInstance(entries, list)
        assert isinstance(entries, list)
        named_figures: set[str] = set()
        for index, value in enumerate(entries):
            with self.subTest(entry=index):
                self.assertIsInstance(value, Mapping)
                assert isinstance(value, Mapping)
                self.assertTrue(set(value).issubset(triggers._ENTRY_KEYS))
                state = value["state"]
                measure = value["measure"]
                self.assertIn(state, triggers.STATES)
                self.assertIn(measure, triggers.MEASURES)
                condition = value.get("condition")
                if condition is None:
                    continue
                self.assertIsInstance(condition, Mapping)
                assert isinstance(condition, Mapping)
                clauses = condition.get("all", [condition])
                if "all" in condition:
                    self.assertEqual(set(condition), set(triggers._CONDITION_KEYS))
                self.assertIsInstance(clauses, list)
                assert isinstance(clauses, list)
                for clause in clauses:
                    self.assertIsInstance(clause, Mapping)
                    assert isinstance(clause, Mapping)
                    self.assertTrue(set(clause).issubset(triggers._CLAUSE_KEYS))
                    figure = clause["figure"]
                    operation = clause["op"]
                    self.assertIsInstance(figure, str)
                    self.assertIn(operation, triggers.OPS)
                    if isinstance(figure, str):
                        named_figures.add(figure)
                    if measure != "unavailable":
                        self.assertIn(figure, triggers.MEASURES[measure])

        for measure, figures in triggers.MEASURES.items():
            with self.subTest(measure=measure):
                if measure != "unavailable":
                    self.assertTrue(set(figures) & named_figures)


class Validation(unittest.TestCase):
    """Every refusal identifies its path and renders its repair."""

    def assert_errors(self, errors: list[TriggerError], *expected_paths: str) -> None:
        self.assertTrue(errors, "expected a refusal")
        paths = {error.key_path for error in errors}
        for expected_path in expected_paths:
            self.assertIn(expected_path, paths)
        lines = render_errors(errors).splitlines()
        self.assertEqual(len(lines), len(errors))
        for line, error in zip(lines, errors, strict=True):
            self.assertTrue(line.endswith(error.fix))

    def assert_document_errors(
        self, document: dict[str, object], *expected_paths: str
    ) -> None:
        self.assert_errors(validate_trigger_registry(document), *expected_paths)

    def test_valid_seeded_document_has_no_findings(self) -> None:
        self.assertEqual(validate_trigger_registry(_document()), [])

    def test_collects_many_findings_in_one_document(self) -> None:
        document = {
            "version": "one",
            "future": True,
            "triggers": [
                {
                    "id": "Not valid",
                    "reopens": "18.4",
                    "register": "M-18",
                    "measure": "evalset",
                    "state": "watcing",
                    "condition": {
                        "figure": "panel_disagreement_pct",
                        "op": "atleast",
                        "value": True,
                        "runs": 0,
                        "future_clause": True,
                    },
                    "says": "A fictitious malformed trigger.",
                    "future_entry": True,
                },
                _entry(id="duplicate-trigger"),
                _entry(id="duplicate-trigger"),
            ],
        }
        errors = validate_trigger_registry(document)
        self.assert_errors(
            errors,
            "future",
            "triggers[0].future_entry",
            "triggers[0].condition.future_clause",
            "triggers[0].id",
            "triggers[0].reopens",
            "triggers[0].register",
            "triggers[0].measure",
            "triggers[0].state",
            "triggers[0].condition.op",
            "triggers[0].condition.value",
            "triggers[0].condition.runs",
            "triggers[2].id",
        )
        self.assertIn(
            "expected a section reference: the section sign, then a number such as 18.4",
            next(
                error.problem
                for error in errors
                if error.key_path == "triggers[0].reopens"
            ),
        )

    def test_missing_required_key_is_reported(self) -> None:
        document = _document()
        entry = document["triggers"]
        assert isinstance(entry, list)
        del entry[0]["id"]
        self.assert_document_errors(document, "triggers[0].id")

    def test_wrong_types_are_reported(self) -> None:
        cases = (
            ({"version": "one"}, "version"),
            ({"version": 2}, "version"),
            ({"triggers": "not-a-list"}, "triggers"),
            ({"triggers": ["not-a-mapping"]}, "triggers[0]"),
            (None, "triggers[0].says"),
        )
        for overrides, path in cases:
            with self.subTest(path=path):
                document = (
                    _entry_document(says=3)
                    if overrides is None
                    else _document(**overrides)
                )
                self.assert_document_errors(document, path)

    def test_unknown_keys_report_nearest_valid_keys(self) -> None:
        document = _document()
        entry = document["triggers"]
        assert isinstance(entry, list)
        document["verison"] = True
        entry[0]["futur"] = True
        condition = entry[0]["condition"]
        assert isinstance(condition, dict)
        condition["figur"] = "judged_queries"
        errors = validate_trigger_registry(document)
        self.assert_errors(
            errors,
            "verison",
            "triggers[0].futur",
            "triggers[0].condition.figur",
        )
        problems = {error.key_path: error.problem for error in errors}
        self.assertIn("version", problems["verison"])
        self.assertIn("figure", problems["triggers[0].condition.figur"])

    def test_unknown_vocabulary_words_report_nearest_words(self) -> None:
        cases = (
            ({"state": "watcing"}, "triggers[0].state", "watching"),
            (
                {
                    "condition": {
                        "figure": "judged_queries",
                        "op": "atleast",
                        "value": 1,
                    }
                },
                "triggers[0].condition.op",
                "at-least",
            ),
            ({"measure": "evalset"}, "triggers[0].measure", "eval-set"),
        )
        for overrides, path, nearest in cases:
            with self.subTest(path=path):
                document = _entry_document(**overrides)
                errors = validate_trigger_registry(document)
                self.assert_errors(errors, path)
                self.assertIn(
                    nearest,
                    next(error.problem for error in errors if error.key_path == path),
                )

    def test_malformed_patterns_are_reported(self) -> None:
        cases = (
            ({"id": "Not valid"}, "triggers[0].id"),
            ({"reopens": "18.4"}, "triggers[0].reopens"),
            ({"register": "M-18"}, "triggers[0].register"),
            (
                {"state": "acted", "condition": None, "ruled": "not a ruling"},
                "triggers[0].ruled",
            ),
        )
        for overrides, path in cases:
            with self.subTest(path=path):
                self.assert_document_errors(_entry_document(**overrides), path)

    def test_measure_figure_mismatch_is_reported(self) -> None:
        self.assert_document_errors(
            _entry_document(
                condition={
                    "figure": "panel_disagreement_pct",
                    "op": "above",
                    "value": 1,
                }
            ),
            "triggers[0].condition.figure",
        )

    def test_state_rules_are_reported(self) -> None:
        cases = (
            (
                _entry(state="acted", ruled="2099-01-01 PR-example"),
                "triggers[0].condition",
            ),
            (
                _entry(state="watching", ruled="2099-01-01 PR-example"),
                "triggers[0].ruled",
            ),
            (
                _entry(state="watching", condition=None),
                "triggers[0].condition",
            ),
            (
                _entry(state="retired", condition=None),
                "triggers[0].ruled",
            ),
        )
        for entry, path in cases:
            with self.subTest(path=path):
                self.assert_document_errors(_document(entry), path)

    def test_valid_state_shapes_match_the_state_rules(self) -> None:
        acted = _entry(state="acted", condition=None, ruled="2099-01-01 PR-example")
        retired = _entry(state="retired", condition=None, ruled="2099-01-01 PR-example")
        cases = (_entry(state="watching"), acted, retired)
        for entry in cases:
            with self.subTest(state=entry["state"]):
                self.assertEqual(validate_trigger_registry(_document(entry)), [])

    def test_empty_all_is_reported(self) -> None:
        self.assert_document_errors(
            _entry_document(condition={"all": []}), "triggers[0].condition.all"
        )

    def test_runs_below_one_is_reported(self) -> None:
        self.assert_document_errors(
            _entry_document(
                condition={
                    "figure": "judged_queries",
                    "op": "at-least",
                    "value": 1,
                    "runs": 0,
                }
            ),
            "triggers[0].condition.runs",
        )

    def test_boolean_value_is_reported(self) -> None:
        self.assert_document_errors(
            _entry_document(
                condition={
                    "figure": "judged_queries",
                    "op": "at-least",
                    "value": True,
                }
            ),
            "triggers[0].condition.value",
        )

    def test_duplicate_id_reports_the_second_index(self) -> None:
        self.assert_document_errors(
            _document(_entry(id="same-trigger"), _entry(id="same-trigger")),
            "triggers[1].id",
        )


class Loading(unittest.TestCase):
    """The Host seam turns every read and parse refusal into one finding."""

    def assert_errors(self, errors: list[TriggerError]) -> None:
        self.assertTrue(errors, "expected a refusal")
        lines = render_errors(errors).splitlines()
        self.assertEqual(len(lines), len(errors))
        for line, error in zip(lines, errors, strict=True):
            self.assertTrue(line.endswith(error.fix))

    def assert_read_failure(
        self, error: BaseException | None, text: str | None
    ) -> None:
        files = {} if text is None else {FAKE_PATH: text}
        errors = {} if error is None else {FAKE_PATH: error}
        host = DictHost(files=files, errors=errors)
        result = load_trigger_registry(FAKE_PATH, host=host)
        self.assertFalse(result.ok)
        self.assertIsNone(result.registry)
        self.assertEqual(len(result.errors), 1)
        self.assertIsNone(result.errors[0].key_path)
        self.assert_errors(list(result.errors))

    def test_read_failures_are_each_reported_once(self) -> None:
        cases = (
            ("missing", None, None),
            ("permissions", PermissionError(FAKE_PATH), None),
            (
                "utf8",
                UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
                None,
            ),
            ("os error", OSError("read failure"), None),
            ("empty", None, ""),
            ("non-mapping", None, "[]"),
            ("unparsable", None, "version: ["),
            ("duplicate key", None, "version: 1\nversion: 1\n"),
        )
        for name, error, text in cases:
            with self.subTest(name=name):
                self.assert_read_failure(error, text)
