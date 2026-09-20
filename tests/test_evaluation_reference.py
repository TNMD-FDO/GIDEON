"""Reference-file format, reader findings, and per-case comparison contracts."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast

from gideon.evaluation import reference
from gideon.evaluation.evalset import SET_ROOT, LoadedSet, load_set
from gideon.evaluation.extraction_slice import run_extraction
from gideon.evaluation.results import RunContext
from gideon.host import courts
from gideon.host.sysio import RealHost
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]

LISTS = {
    "cases": ("extraction-001", "extraction-002", "extraction-003", "extraction-004"),
    "other": ("extraction-005",),
}
VERSION = "eval-v-test"
PRODUCT = "0.0.0-test"
TAG = "v0.0.0-test"
DIGEST = "f" * 64


def _file(
    list_name: str = "cases",
    *,
    cases: dict[str, reference.Verdict] | None = None,
    eval_set_version: str = VERSION,
    hardware_profile: str = "fictitious-profile",
) -> reference.ReferenceFile:
    return reference.ReferenceFile(
        format=reference.FORMAT_VERSION,
        product_version=PRODUCT,
        corpus_lockfile=None,
        eval_set_version=eval_set_version,
        hardware_profile=hardware_profile,
        tag=TAG,
        slice="extraction",
        list=list_name,
        repeats=2,
        set_digest=DIGEST,
        cases={} if cases is None else cases,
    )


def _write(root: Path, value: reference.ReferenceFile, *, text: str | None = None) -> Path:
    path = root / reference.REFERENCE_ROOT / value.slice / f"{value.list}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        reference.serialize_reference(value) if text is None else text,
        encoding="utf-8",
    )
    return path


class Comparison(unittest.TestCase):
    """The four list rules and the closed outcome vocabulary are content-free."""

    def test_the_five_list_cases_are_derived_by_id(self) -> None:
        loaded = reference.SliceReference(
            (
                _file(
                    cases={
                        "extraction-001": "pass",
                        "extraction-002": "fail",
                        "extraction-004": "pass",
                    }
                ),
            )
        )
        result = reference.compare_reference(
            loaded,
            {
                "extraction-001": "fail",
                "extraction-002": "pass",
                "extraction-003": "pass",
            },
            VERSION,
        )
        self.assertEqual(result.outcome, "regressed")
        self.assertEqual(result.regressed, ("extraction-001",))
        self.assertEqual(result.gained, ("extraction-002",))
        self.assertEqual(result.new, ("extraction-003",))
        self.assertEqual(result.dropped, ("extraction-004",))
        self.assertTrue(result.stale)

    def test_another_version_is_refused_with_its_tag(self) -> None:
        result = reference.compare_reference(
            reference.SliceReference((_file(eval_set_version="eval-v-fictitious-new"),)),
            {},
            VERSION,
        )
        self.assertEqual(result.outcome, "other-version")
        self.assertEqual(result.tag, TAG)
        self.assertEqual(result.regressed, ())

    def test_repeated_verdicts_need_every_repeat_to_pass(self) -> None:
        self.assertEqual(
            reference.fold_repeats(
                (
                    ("extraction-001", 1, "pass"),
                    ("extraction-001", 2, "pass"),
                    ("extraction-002", 1, "pass"),
                    ("extraction-002", 2, "fail"),
                )
            ),
            {"extraction-001": "pass", "extraction-002": "fail"},
        )

    def test_regressed_outcome_outranks_stale(self) -> None:
        result = reference.compare_reference(
            reference.SliceReference(
                (
                    _file(
                        cases={
                            "extraction-001": "pass",
                            "extraction-002": "fail",
                        }
                    ),
                )
            ),
            {
                "extraction-001": "fail",
                "extraction-002": "pass",
                "extraction-003": "pass",
            },
            VERSION,
        )
        self.assertEqual(result.outcome, "regressed")
        self.assertTrue(result.stale)

    def test_outcome_vocabulary_is_closed(self) -> None:
        self.assertEqual(
            set(reference.OUTCOMES),
            {"current", "stale", "absent", "other-version", "regressed", "malformed"},
        )


class Serialization(unittest.TestCase):
    """The file is canonical JSON rather than an editable report."""

    def test_serialization_is_key_ordered_ascii_and_newline_terminated(self) -> None:
        text = reference.serialize_reference(
            _file(
                cases={
                    "extraction-002": "fail",
                    "extraction-001": "pass",
                }
            )
        )
        document = json.loads(text)
        self.assertEqual(tuple(document), tuple(sorted(document)))
        self.assertEqual(tuple(document["cases"]), ("extraction-001", "extraction-002"))
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text, reference.serialize_reference(_file(cases=dict(reversed(tuple(document["cases"].items()))))))


class Reader(unittest.TestCase):
    """The reader collects content-free findings and distinguishes absence."""

    def test_no_reference_is_absent_not_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = reference.read_reference(directory, "extraction", LISTS, VERSION)
        self.assertIsNone(result.reference)
        self.assertEqual(result.findings, ())

    def test_bad_file_and_case_findings_name_locations_not_case_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = _file(cases={"extraction-001": "pass"})
            path = _write(Path(directory), value)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["cases"]["extraction-001"] = "maybe"
            document["question"] = "[FICTIONAL TEST QUESTION — never print this]"
            path.write_text(json.dumps(document), encoding="utf-8")
            result = reference.read_reference(directory, "extraction", {"cases": LISTS["cases"]}, VERSION)
        rendered = "\n".join(finding.text() for finding in result.findings)
        self.assertIsNone(result.reference)
        self.assertIn("eval/reference/extraction/cases.json", rendered)
        self.assertIn("id extraction-001", rendered)
        self.assertNotIn("FICTIONAL TEST QUESTION", rendered)

    def test_case_findings_use_file_and_id_without_case_text(self) -> None:
        cases: tuple[tuple[dict[str, reference.Verdict], str], ...] = (
            ({"extraction-001": cast(reference.Verdict, "maybe")}, "extraction-001"),
            ({"not-a-case": "pass"}, "not-a-case"),
            ({"extraction-005": "pass"}, "extraction-005"),
        )
        for case_values, case_id in cases:
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                value = _file(cases=case_values)
                path = _write(root, value)
                result = reference.read_reference(
                    root,
                    "extraction",
                    {"cases": LISTS["cases"]},
                    VERSION,
                )
            matching = tuple(finding for finding in result.findings if finding.id == case_id)
            self.assertTrue(matching)
            self.assertTrue(all(finding.file == path.relative_to(root).as_posix() for finding in matching))
            self.assertTrue(all("question" not in finding.rule for finding in matching))

    def test_header_disagreement_across_slice_files_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, _file(cases={"extraction-001": "pass"}))
            _write(root, _file("other", cases={"extraction-005": "pass"}, hardware_profile="other-profile"))
            result = reference.read_reference(root, "extraction", LISTS, VERSION)
        self.assertIsNone(result.reference)
        self.assertTrue(any("hardware_profile disagrees" in finding.rule for finding in result.findings))
        self.assertTrue(any(finding.file.endswith("other.json") for finding in result.findings))

    def test_partial_reference_is_malformed_not_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _write(Path(directory), _file(cases={"extraction-001": "pass"}))
            result = reference.read_reference(directory, "extraction", LISTS, VERSION)
        self.assertIsNone(result.reference)
        self.assertTrue(any("partial reference" in finding.rule for finding in result.findings))

    def test_hand_edited_bytes_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = _file(cases={"extraction-001": "pass"})
            path = _write(root, value)
            path.write_text(path.read_text(encoding="utf-8").replace("\n", "\n\n", 1), encoding="utf-8")
            result = reference.read_reference(root, "extraction", {"cases": ("extraction-001",)}, VERSION)
        self.assertIsNone(result.reference)
        self.assertTrue(any("canonical serialization" in finding.rule for finding in result.findings))

    def test_another_versions_files_are_never_judged_by_this_sets_id_lists(self) -> None:
        """A set-version bump reaches the writer, not the restore-or-remove fix.

        The checks that read the loaded set's id lists say nothing about a
        reference recorded under another version, so they must not condemn it
        as malformed before the comparison can call it other-version.
        """

        older = "eval-v-fictitious-older"
        cases: tuple[tuple[str, dict[str, reference.Verdict]], ...] = (
            # an id this set's list no longer names
            ("cases", {"extraction-001": "pass", "extraction-099": "fail"}),
            # a whole id list this set no longer has
            ("gone", {"extraction-001": "pass"}),
        )
        for list_name, case_values in cases:
            with self.subTest(list_name=list_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _write(root, _file(list_name, cases=case_values, eval_set_version=older))
                result = reference.read_reference(
                    root, "extraction", {"cases": ("extraction-001",)}, VERSION
                )
                self.assertEqual(result.findings, ())
                self.assertIsNotNone(result.reference)
                comparison = reference.compare_reference(result.reference, {}, VERSION)
                self.assertEqual(comparison.outcome, "other-version")

    def test_a_mixed_version_slice_is_malformed_on_its_header_disagreement(self) -> None:
        """Files left behind by a bump disagree, so removal stays deliberate."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root, _file("cases", cases={"extraction-001": "pass"}))
            _write(root, _file("other", cases={}, eval_set_version="eval-v-fictitious-older"))
            result = reference.read_reference(root, "extraction", LISTS, VERSION)
        self.assertIsNone(result.reference)
        self.assertTrue(
            any("eval_set_version disagrees" in finding.rule for finding in result.findings),
            tuple(finding.rule for finding in result.findings),
        )


class CommittedReferences(unittest.TestCase):
    """The files under eval/reference/ are a contract, not a convenience.

    A kept file's absence is never tolerated (slice-1 tickets 57 and 79); only
    the harvest reference may be missing, and only in an export tree, where it
    leaves with the id list and the cases it names.
    """

    def _loaded(self) -> LoadedSet:
        court_result = courts.load_court_map(courts.default_courts_path())
        assert court_result.court_map is not None
        result = load_set(ROOT / SET_ROOT, court_result.court_map.courts)
        self.assertEqual(result.findings, ())
        assert result.loaded is not None
        return result.loaded

    def test_the_committed_reference_loads_without_a_finding(self) -> None:
        loaded = self._loaded()
        slice_lists = dict(loaded.slice_lists["extraction"])
        harvest_gone = absent_from_export("eval/reference/extraction/harvest.json", ROOT)
        if harvest_gone:
            slice_lists.pop("harvest", None)
        else:
            self.assertTrue((ROOT / "eval" / "reference" / "extraction" / "harvest.json").is_file())
        self.assertTrue((ROOT / "eval" / "reference" / "extraction" / "invented.json").is_file())

        result = reference.read_reference(
            ROOT, "extraction", slice_lists, loaded.version
        )
        self.assertEqual(
            tuple(finding.text() for finding in result.findings),
            (),
        )
        self.assertIsNotNone(result.reference)

    def test_the_committed_reference_is_current_against_the_committed_set(self) -> None:
        loaded = self._loaded()
        slice_result = run_extraction(loaded, "extraction", _engine_free_context())
        current = reference.fold_repeats(
            tuple((case.case_id, case.repeat, case.verdict) for case in slice_result.results)
        )
        slice_lists = dict(loaded.slice_lists["extraction"])
        if absent_from_export("eval/reference/extraction/harvest.json", ROOT):
            slice_lists.pop("harvest", None)
            current = {
                case_id: verdict
                for case_id, verdict in current.items()
                if case_id in set(slice_lists.get("invented", ()))
            }
        result = reference.read_reference(
            ROOT, "extraction", slice_lists, loaded.version
        )
        comparison = reference.compare_reference(
            result.reference, current, loaded.version
        )
        # Nothing may regress against the committed reference. Staleness is not
        # a failure: a cycle that grows the set moves gained/new/dropped, and
        # the release re-records at its own tag (ruling (d)).
        self.assertEqual(comparison.regressed, ())
        self.assertIn(comparison.outcome, ("current", "stale"), comparison)

    def test_a_planted_regression_fails_against_the_committed_reference(self) -> None:
        """The hosted half of criterion 4: the bounds pass, the list still blocks."""

        loaded = self._loaded()
        result = reference.read_reference(
            ROOT,
            "extraction",
            loaded.slice_lists["extraction"],
            loaded.version,
        )
        assert result.reference is not None
        passing = {
            case_id
            for file in result.reference.files
            for case_id, verdict in file.cases.items()
            if verdict == "pass"
        }

        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "eval-v1"
            shutil.copytree(ROOT / SET_ROOT, copy)
            planted = _plant_one_key(copy, passing)
            court_result = courts.load_court_map(courts.default_courts_path())
            assert court_result.court_map is not None
            planted_result = load_set(copy, court_result.court_map.courts)
            self.assertEqual(planted_result.findings, ())
            assert planted_result.loaded is not None
            slice_result = run_extraction(
                planted_result.loaded, "extraction", _engine_free_context()
            )

        current = reference.fold_repeats(
            tuple((case.case_id, case.repeat, case.verdict) for case in slice_result.results)
        )
        comparison = reference.compare_reference(result.reference, current, loaded.version)
        self.assertEqual(comparison.outcome, "regressed")
        self.assertIn(planted, comparison.regressed)
        # The scorer's own bounds still hold — the failure a mean hides.
        self.assertTrue(slice_result.verdict)


def _engine_free_context() -> RunContext:
    """The context of a slice that reaches no engine; the extraction runner ignores it."""

    return RunContext(RealHost(), "/rendered", None, None, 1, lambda _line: None)


def _plant_one_key(set_root: Path, passing: set[str]) -> str:
    """Flip one reference-passing case's keyed object, ticket 04's technique."""

    for path in sorted(set_root.glob("build-gates/*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            case = json.loads(line)
            if case.get("id") not in passing:
                continue
            for obj in case.get("expected", {}).get("objects", []):
                if isinstance(obj.get("key"), str) and obj["key"]:
                    obj["key"] = obj["key"] + "zz"
                    lines[index] = json.dumps(case, ensure_ascii=False) + "\n"
                    path.write_text("".join(lines), encoding="utf-8")
                    return cast(str, case["id"])
    raise AssertionError("no reference-passing case with a keyed object was found")


if __name__ == "__main__":
    unittest.main()
