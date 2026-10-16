"""Gold document intake and sample command loading over fictitious eval sets."""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_evaluation_set import (
    _judgment,
    _judgment_case,
    _research_qa_case,
    _write,
    _write_research_cases,
    _write_signoffs,
)

from gideon.evaluation import evalset, judgments, sample, signoffs
from gideon.host.index.generations import COLLECTIONS
from gideon.host.index.sample import SAMPLE_FILL_PER_MILLION, GoldDocument, SampleInputs

PRIVATE = "FICTITIOUS PRIVATE QUESTION AND ANSWER SENTINEL"


class GoldIntake(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.checkout = Path(directory.name)
        self.root = self.checkout / evalset.SET_ROOT
        cases = tuple(_judgment_case(f"judgments-{number:03}", question=PRIVATE) for number in range(1, 4))
        _write(self.root, "judgments/queries.jsonl", "\n".join(json.dumps(case) for case in cases))
        self.judged = (
            replace(_judgment("judgments-001"), source_id="fictitious:opinion:9", sha256="b" * 64),
            replace(_judgment("judgments-002"), source_id="fictitious:opinion:1", sha256="a" * 64),
            replace(_judgment("judgments-003"), source_id="fictitious:opinion:9", sha256="b" * 64),
        )
        _write(self.root, judgments.JUDGMENTS_PATH.as_posix(),
               "".join(judgments.serialize(row) for row in self.judged).rstrip("\n"))
        _write_research_cases(self.root, (_research_qa_case(question=PRIVATE),))
        self.signed = signoffs.SignOff(
            "research-qa-001", PRIVATE,
            ("fictitious:opinion:9", "fictitious:opinion:5", "fictitious:opinion:1"),
            "CHU-attorney-1", "2099-01-03",
        )
        _write_signoffs(self.root, (self.signed,))
        self.args = argparse.Namespace(collection=COLLECTIONS[0].name, sample=True, lockfile=None)

    def load(self) -> evalset.LoadedSet:
        result = evalset.load_set(self.root)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        return result.loaded

    def test_gold_is_sorted_deduplicated_and_keeps_judged_hashes(self) -> None:
        loaded = self.load()
        self.assertEqual(sample.gold_documents(loaded), (
            GoldDocument("fictitious:opinion:1", self.judged[1].sha256),
            GoldDocument("fictitious:opinion:5", None),
            GoldDocument("fictitious:opinion:9", self.judged[0].sha256),
        ))
        self.assertEqual(sample.gold_documents(replace(loaded, judgments=(), signoffs=())), ())
        self.assertEqual(sample.gold_documents(replace(loaded, judgments=())), tuple(
            GoldDocument(identifier, None) for identifier in sorted(self.signed.must_cite)
        ))

    def test_gold_hashes_keep_every_judged_hash_and_the_same_sorted_ids_as_documents(self) -> None:
        self.judged = (*self.judged[:2], replace(self.judged[2], sha256="c" * 64))
        _write(self.root, judgments.JUDGMENTS_PATH.as_posix(),
               "".join(judgments.serialize(row) for row in self.judged).rstrip("\n"))
        loaded = self.load()
        hashes = sample.gold_hashes(loaded)
        self.assertEqual(hashes, {
            self.judged[1].source_id: frozenset({self.judged[1].sha256}),
            self.signed.must_cite[1]: frozenset(),
            self.judged[0].source_id: frozenset({self.judged[0].sha256, self.judged[2].sha256}),
        })
        self.assertEqual(tuple(hashes), tuple(sorted(hashes)))
        documents = sample.gold_documents(loaded)
        self.assertEqual(tuple(hashes), tuple(document.source_id for document in documents))
        self.assertEqual(documents[-1].sha256, self.judged[2].sha256)
        self.assertEqual(sample.gold_hashes(replace(loaded, judgments=(), signoffs=())), {})
        self.assertEqual(sample.gold_hashes(replace(loaded, judgments=())), {
            source_id: frozenset() for source_id in sorted(self.signed.must_cite)
        })

    def test_sample_command_loads_the_release_set_and_passes_inputs_and_exit_code(self) -> None:
        loaded = self.load()
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(sample, "__file__", str(self.checkout / "gideon/evaluation/sample.py")),
            patch.object(sample.build, "run_index_build", return_value=3) as build,
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(sample.run_sample_build(self.args), 3)
        build.assert_called_once_with(self.args, sample=SampleInputs(
            sample.gold_documents(loaded), SAMPLE_FILL_PER_MILLION, loaded.version,
        ))
        self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
        self.assertNotIn(PRIVATE, repr(build.call_args))

    def test_loader_findings_refuse_before_build_and_hide_case_values(self) -> None:
        case = _judgment_case(question=PRIVATE)
        case["review"] = {"by": PRIVATE, "on": "2099-01-03", "accepted_flags": []}
        _write(self.root, "judgments/queries.jsonl", json.dumps(case))
        result = evalset.load_set(self.root)
        self.assertTrue(result.findings)
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(sample, "__file__", str(self.checkout / "gideon/evaluation/sample.py")),
            patch.object(sample.build, "run_index_build") as build,
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(sample.run_sample_build(self.args), 1)
        build.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue().splitlines(), [finding.text() for finding in result.findings])
        self.assertIn("judgments/queries.jsonl:id judgments-001", errors.getvalue())
        self.assertIn("Fix:", errors.getvalue())
        self.assertNotIn(PRIVATE, errors.getvalue())
