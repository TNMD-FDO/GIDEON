"""Production smoke evidence contracts without a stack."""

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from test_index_record import FakeHost

from gideon.evaluation import record
from gideon.host import report, stack, worker
from gideon.host.index import generations, smoke
from gideon.host.index.generations import GenerationInputs
from gideon.host.index.sample import sample_name
from gideon.host.report import Problem
from gideon.host.sysio import PathLike

RENDERED = "/fictitious-rendered"
RUN_ID = str(UUID(int=1))
FINISHED = datetime(2099, 1, 3, 12, 34, 56, tzinfo=UTC)
RESTORE_FIX = "Restore the reference file from the release checkout, then retry."


def inputs_for() -> GenerationInputs:
    """Own visibly fictitious build inputs, independent of movable lock pins."""

    label, space = "corpus-2099-01-03", "example-embedding-space"
    return GenerationInputs(
        generations.generation_name("caselaw", label, space), "caselaw", label,
        "fictions-2099-01-02", (), {}, "1000.0.0", space, "a" * 64,
    )


def reference_for() -> dict[str, object]:
    inputs = inputs_for()
    return {
        "format": 2, "slice": "smoke", "corpus_lockfile": inputs.label,
        "generation_id": smoke.expected_sample(inputs), "verdict": "pass",
        "tag": "v1000.0.0",
        # The build's reader has no contract with the cases payload.
        "cases": "fictitious payload, deliberately not a case list",
    }


def row_for() -> dict[str, object]:
    return {
        "run_id": RUN_ID, "stack": "ci", "kind": "smoke", "finished_at": FINISHED.isoformat(),
    }


class ReferenceHost(FakeHost):
    """Read only an owned temporary checkout, with all subprocesses recorded."""

    def __init__(self, checkout: Path) -> None:
        super().__init__()
        self.checkout = checkout

    def listdir(self, path: PathLike) -> list[str]:
        target = Path(path)
        assert target.is_relative_to(self.checkout)
        return [entry.name for entry in target.iterdir()]

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        target = Path(path)
        assert target.is_relative_to(self.checkout)
        return target.read_text(encoding=encoding)


class ExpectedSample(unittest.TestCase):
    """Evidence is for this label's sample, including an instrument cut."""

    def test_name_uses_requested_components_instead_of_converged_row_name(self) -> None:
        inputs = inputs_for()
        converged = replace(inputs, label="corpus-2099-01-04")
        self.assertEqual(smoke.expected_sample(converged), sample_name(generations.generation_name(
            converged.collection, converged.label, converged.embedding_space,
        )))
        self.assertNotEqual(smoke.expected_sample(converged), sample_name(converged.name))
        changed = replace(converged, embedding_space="example-other-space")
        self.assertNotEqual(smoke.expected_sample(changed), smoke.expected_sample(converged))


class _ReferenceFixture(unittest.TestCase):
    """Own a temporary reference directory and the host that reads it."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name)
        self.directory = self.checkout / "eval/reference/smoke"
        self.host = ReferenceHost(self.checkout)
        self.inputs = inputs_for()
        self.sample = smoke.expected_sample(self.inputs)

    def write_reference(self, document: object, name: str = "v1000.0.0.json") -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def read(self) -> smoke.SmokeEvidence | None | Problem:
        return smoke.read_reference_evidence(
            self.host, self.checkout, label=self.inputs.label, sample=self.sample,
        )


class References(_ReferenceFixture):
    """Only a format-2 passing smoke header naming the sample is evidence."""

    def test_absent_empty_and_non_json_directories_have_no_evidence(self) -> None:
        self.assertIsNone(self.read())
        self.directory.mkdir(parents=True)
        self.assertIsNone(self.read())
        (self.directory / "notes.txt").write_text("fictitious non-reference", encoding="utf-8")
        self.assertIsNone(self.read())
        self.assertEqual(self.host.calls, [])

    def test_format_one_is_ignored_even_when_its_other_headers_match(self) -> None:
        document = reference_for()
        document["format"] = 1
        del document["generation_id"]
        del document["verdict"]
        self.write_reference(document)
        self.assertIsNone(self.read())

    def test_export_without_harvest_matches_release_reference_without_reading_cases(self) -> None:
        document = reference_for()
        path = self.write_reference(document)
        evidence = self.read()
        assert isinstance(evidence, smoke.SmokeEvidence)
        self.assertEqual((evidence.source, evidence.label, evidence.sample), (
            "reference", self.inputs.label, self.sample,
        ))
        self.assertEqual(evidence.detail, f"reference {document['tag']} ({path.name})")
        self.assertFalse((self.directory / "harvest.json").exists())
        self.assertEqual(self.host.calls, [])

    def test_failing_other_slice_other_lockfile_and_other_generation_do_not_match(self) -> None:
        for key, value in (
            ("verdict", "fail"), ("slice", "example-other-slice"),
            ("corpus_lockfile", "corpus-2099-01-04"),
            ("generation_id", self.sample + "-other"),
        ):
            with self.subTest(key=key):
                self.write_reference({**reference_for(), key: value})
                self.assertIsNone(self.read())

    def test_nonobjects_unknown_formats_and_keyless_objects_refuse_with_restore_fix(self) -> None:
        document: object
        for document in (None, [], "fictitious", {}, {"format": 3}, {"format": True}, {"format": 2.0}):
            with self.subTest(document=document):
                path = self.write_reference(document)
                result = self.read()
                assert isinstance(result, Problem)
                self.assertIn(str(path), result.problem)
                self.assertEqual(result.fix, RESTORE_FIX)

    def test_each_format_two_header_must_be_present_and_a_string(self) -> None:
        value: object
        for key in ("slice", "corpus_lockfile", "generation_id", "verdict", "tag"):
            for value in (None, 1, False, [], {}):
                with self.subTest(key=key, value=value):
                    path = self.write_reference({**reference_for(), key: value})
                    result = self.read()
                    assert isinstance(result, Problem)
                    self.assertIn(str(path), result.problem)
                    self.assertEqual(result.fix, RESTORE_FIX)
            with self.subTest(missing=key):
                document = reference_for()
                del document[key]
                self.write_reference(document)
                self.assertIsInstance(self.read(), Problem)

    def test_invalid_json_and_unreadable_files_refuse(self) -> None:
        path = self.write_reference(reference_for())
        path.write_text("{fictitious broken JSON", encoding="utf-8")
        result = self.read()
        assert isinstance(result, Problem)
        self.assertIn(str(path), result.problem)
        self.assertEqual(result.fix, RESTORE_FIX)
        for error in (PermissionError(), FileNotFoundError(), UnicodeError()):
            with self.subTest(error=error), patch.object(self.host, "read_text", side_effect=error):
                result = self.read()
                assert isinstance(result, Problem)
                self.assertIn(str(path), result.problem)
                self.assertEqual(result.fix, RESTORE_FIX)

    def test_unreadable_directory_refuses_with_restore_fix(self) -> None:
        with patch.object(self.host, "listdir", side_effect=PermissionError()):
            result = self.read()
        assert isinstance(result, Problem)
        self.assertIn(str(self.directory), result.problem)
        self.assertEqual(result.fix, RESTORE_FIX)

    def test_malformed_reference_refuses_even_beside_matching_evidence(self) -> None:
        self.write_reference(reference_for(), "a-match.json")
        broken = self.write_reference({}, "z-broken.json")
        result = self.read()
        assert isinstance(result, Problem)
        self.assertIn(str(broken), result.problem)


class Record(unittest.TestCase):
    """A read-only, stdin-bound query accepts complete passing sample runs."""

    def read(self, host: FakeHost) -> smoke.SmokeEvidence | None | Problem:
        inputs = inputs_for()
        return smoke.read_record_evidence(
            host, RENDERED, label=inputs.label, sample=smoke.expected_sample(inputs),
        )

    def test_reader_argv_equals_the_evaluation_metrics_reader(self) -> None:
        self.assertEqual(smoke.reader_argv(RENDERED), record.reader_argv(RENDERED))

    def test_query_binds_values_and_filters_complete_passing_runs_before_newest_limit(self) -> None:
        host = FakeHost()
        label, sample = "fictitious 'label\\value", "fictitious 'sample\\value"
        self.assertIsNone(smoke.read_record_evidence(host, RENDERED, label=label, sample=sample))
        self.assertEqual(len(host.calls), 1)
        argv, sql = host.calls[0]
        self.assertEqual(argv, record.reader_argv(RENDERED))
        assert sql is not None
        self.assertEqual(sql.splitlines()[:2], [worker.bind("v_label", label), worker.bind("v_sample", sample)])
        self.assertEqual(sql.count("SELECT "), 1)
        for clause in (
            "FROM public.eval_runs", "slice = 'smoke'", "verdict = 'pass'", "partial = false",
            "corpus_lockfile = :'v_label'", "generation_id = :'v_sample'",
            "ORDER BY started_at DESC", "LIMIT 1",
        ):
            self.assertIn(clause, sql)
        self.assertLess(sql.index("ORDER BY"), sql.index("LIMIT 1"))
        self.assertNotIn("forced", sql)
        self.assertNotIn(label, " ".join(argv))
        self.assertNotIn(sample, " ".join(argv))

    def test_empty_output_is_no_evidence(self) -> None:
        for stdout in ("", " \n\t"):
            with self.subTest(stdout=stdout):
                self.assertIsNone(self.read(FakeHost(stdout=stdout)))

    def test_matching_row_reports_run_identity_and_utc_finished_minute(self) -> None:
        inputs = inputs_for()
        for stack_name in ("ci", "production"):
            for kind in ("smoke", "nightly", "weekly-off", "decision", "candidate", "engine-verify", "manual"):
                with self.subTest(stack=stack_name, kind=kind):
                    row = {**row_for(), "stack": stack_name, "kind": kind}
                    evidence = self.read(FakeHost(stdout=json.dumps(row)))
                    assert isinstance(evidence, smoke.SmokeEvidence)
                    self.assertEqual((evidence.source, evidence.label, evidence.sample), (
                        "record", inputs.label, smoke.expected_sample(inputs),
                    ))
                    self.assertEqual(evidence.detail, (
                        f"run {RUN_ID} ({stack_name}, {kind}, finished {FINISHED:%Y-%m-%dT%H:%M}Z)"
                    ))
        row = {**row_for(), "finished_at": "2099-01-03T14:34:56+02:00"}
        evidence = self.read(FakeHost(stdout=json.dumps(row)))
        assert isinstance(evidence, smoke.SmokeEvidence)
        self.assertIn(f"finished {FINISHED:%Y-%m-%dT%H:%M}Z", evidence.detail)

    def test_malformed_documents_and_rows_refuse_with_postgres_logs_fix(self) -> None:
        documents: list[object] = [None, [], "fictitious", {}, {**row_for(), "unexpected": 1}]
        value: object
        for key in row_for():
            documents.append({name: value for name, value in row_for().items() if name != key})
            for value in (None, 1, False, [], {}):
                documents.append({**row_for(), key: value})
        documents.extend({**row_for(), key: value} for key, value in (
            ("run_id", "fictitious-not-uuid"), ("stack", "example-other-stack"),
            ("kind", "example-other-kind"), ("finished_at", "fictitious-not-time"),
            ("finished_at", "2099-01-03T12:34:56"), ("finished_at", "2099-01-03"),
        ))
        outputs = [json.dumps(document) for document in documents]
        outputs.extend(("fictitious non-JSON", json.dumps(row_for()) + "\n" + json.dumps(row_for())))
        for stdout in outputs:
            with self.subTest(stdout=stdout):
                result = self.read(FakeHost(stdout=stdout))
                assert isinstance(result, Problem)
                self.assertIn("smoke reader", result.problem)
                self.assertEqual(result.fix, f"Run {stack.logs_fix(RENDERED, 'postgres')}, then retry.")

    def test_nonzero_exit_refuses_even_with_a_valid_row_in_stdout(self) -> None:
        for code in (1, 127):
            with self.subTest(code=code):
                host = FakeHost(code=code, stdout=json.dumps(row_for()), stderr="fictitious private diagnostic")
                result = self.read(host)
                assert isinstance(result, Problem)
                self.assertIn(f"exit {code}", result.problem)
                self.assertNotIn(host.stderr, result.problem)
                self.assertEqual(result.fix, f"Run {stack.logs_fix(RENDERED, 'postgres')}, then retry.")
                self.assertEqual(len(host.calls), 1)

    def test_unrunnable_reader_refuses_without_retrying(self) -> None:
        for error in (FileNotFoundError(), PermissionError(), subprocess.SubprocessError()):
            with self.subTest(error=error):
                host = FakeHost(error=error)
                result = self.read(host)
                assert isinstance(result, Problem)
                self.assertIn("could not run", result.problem)
                self.assertEqual(result.fix, f"Run {stack.logs_fix(RENDERED, 'postgres')}, then retry.")
                self.assertEqual(len(host.calls), 1)


class Evidence(_ReferenceFixture):
    """Committed references precede the record; otherwise refuse with the fix."""

    def lookup(self, command_path: str = "index build caselaw") -> smoke.SmokeEvidence | Problem:
        return smoke.smoke_evidence(self.host, RENDERED, self.checkout, self.inputs, command_path)

    def test_reference_precedes_record_and_copies_input_metadata_into_frozen_evidence(self) -> None:
        path = self.write_reference(reference_for())
        self.host.error = FileNotFoundError()
        evidence = self.lookup()
        self.assertEqual(evidence, smoke.SmokeEvidence(
            "reference", self.inputs.label, self.inputs.pipeline, self.inputs.embedding_space,
            self.sample, f"reference {reference_for()['tag']} ({path.name})",
        ))
        assert isinstance(evidence, smoke.SmokeEvidence)
        self.assertEqual(smoke.stage_detail(evidence), (
            f"lockfile {self.inputs.label}, pipeline {self.inputs.pipeline}, "
            f"embedding space {self.inputs.embedding_space}: reference {reference_for()['tag']} ({path.name})"
        ))
        with self.assertRaises(FrozenInstanceError):
            evidence.label = "fictitious"  # type: ignore[misc]
        self.assertEqual(self.host.calls, [])

    def test_record_follows_nonmatching_or_legacy_references(self) -> None:
        self.write_reference({**reference_for(), "verdict": "fail"})
        self.write_reference({"format": 1}, "legacy.json")
        self.host.stdout = json.dumps(row_for())
        evidence = self.lookup()
        assert isinstance(evidence, smoke.SmokeEvidence)
        self.assertEqual(evidence, smoke.SmokeEvidence(
            "record", self.inputs.label, self.inputs.pipeline, self.inputs.embedding_space, self.sample,
            f"run {RUN_ID} (ci, smoke, finished {FINISHED:%Y-%m-%dT%H:%M}Z)",
        ))
        self.assertEqual(smoke.stage_detail(evidence), (
            f"lockfile {self.inputs.label}, pipeline {self.inputs.pipeline}, "
            f"embedding space {self.inputs.embedding_space}: {evidence.detail}"
        ))
        self.assertEqual(len(self.host.calls), 1)

    def test_bad_reference_refuses_before_record_even_if_record_would_match(self) -> None:
        path = self.write_reference({})
        self.host.stdout = json.dumps(row_for())
        result = self.lookup()
        assert isinstance(result, Problem)
        self.assertIn(str(path), result.problem)
        self.assertEqual(result.fix, RESTORE_FIX)
        self.assertEqual(self.host.calls, [])

    def test_record_failure_propagates_instead_of_missing_evidence_fix(self) -> None:
        self.host.code = 127
        result = self.lookup()
        assert isinstance(result, Problem)
        self.assertIn("exit 127", result.problem)
        self.assertEqual(result.fix, f"Run {stack.logs_fix(RENDERED, 'postgres')}, then retry.")

    def test_missing_evidence_names_inputs_and_three_ordered_commands_in_both_forms(self) -> None:
        previous_form = report.command("index build caselaw") == "gideon index build caselaw"
        self.addCleanup(report.set_installed_form, previous_form)
        for installed in (False, True):
            report.set_installed_form(installed)
            prefix = "gideon" if installed else "sudo python3 -m gideon"
            for command in (f"index build caselaw --lockfile {self.inputs.label}", f"corpus install {self.inputs.label}"):
                with self.subTest(installed=installed, command=command):
                    result = self.lookup(command)
                    assert isinstance(result, Problem)
                    self.assertEqual(result.problem, (
                        f"no passing smoke run for lockfile {self.inputs.label}, pipeline {self.inputs.pipeline}, "
                        f"embedding space {self.inputs.embedding_space}: none names generation {self.sample} "
                        "among eval/reference/smoke or the record's runs"
                    ))
                    self.assertEqual(result.fix, (
                        f"Run {prefix} index build caselaw --sample --lockfile {self.inputs.label} on the sibling, "
                        f"then {prefix} eval run --slice smoke --stack ci --kind smoke --generation {self.sample}, "
                        f"then retry {prefix} {command}."
                    ))


class Imports(unittest.TestCase):
    """The evidence reader imports only stdlib and host modules."""

    def test_every_import_is_standard_library_or_host(self) -> None:
        assert smoke.__file__ is not None
        path = Path(smoke.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                targets = [node.module or ""]
            else:
                continue
            for target in targets:
                with self.subTest(target=target, line=node.lineno):
                    self.assertTrue(
                        target == "gideon.host" or target.startswith("gideon.host.")
                        or target.partition(".")[0] in sys.stdlib_module_names,
                        target,
                    )
