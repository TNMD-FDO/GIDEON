"""Eval-set loading contracts from spec §18.6."""

import json
import tempfile
import unittest
from pathlib import Path

from gideon.evaluation.evalset import SET_ROOT, Finding, LoadedSet, load_set
from gideon.extraction.scoring import active_cases
from gideon.host.courts import load_court_map
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "[FICTIONAL TEST QUESTION — never include this text in a finding]"


def _write(root: Path, relative: str, text: str, *, final_newline: bool = True) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + ("\n" if final_newline else ""), encoding="utf-8")


def _case(case_id: str, question: str = SENTINEL) -> str:
    return json.dumps(
        {
            "id": case_id,
            "suite": "build-gates",
            "category": "extraction",
            "branch": "legal",
            "question": question,
            "expected": {"objects": []},
            "labels": ["invented"],
            "cluster_id": case_id,
            "review": {"by": "CSA-1", "on": "2026-09-19"},
            "notes": "",
        },
        separators=(",", ":"),
    )


def _harvest_extraction_case(case_id: str = "extraction-001") -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "build-gates",
        "category": "extraction",
        "branch": "legal",
        "question": SENTINEL,
        "expected": {"objects": []},
        "labels": ["harvest", "doctrinal"],
        "seed": "HARV-001",
        "cluster_id": "harvest-chat-abcdef",
        "review": {"by": "CSA-1", "on": "2026-09-19"},
        "notes": "",
    }


def _variant_case(
    case_id: str = "extraction-002",
    parent: str = "extraction-001",
    axis: str = "sign-present@1",
    cluster_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "build-gates",
        "category": "extraction",
        "branch": "legal",
        "question": SENTINEL,
        "expected": {"objects": []},
        "labels": ["variant", axis],
        "parent": parent,
        "cluster_id": parent if cluster_id is None else cluster_id,
        "review": {"by": "CSA-1", "on": "2026-09-19"},
        "notes": "",
    }


def _write_cases(root: Path, records: tuple[dict[str, object], ...]) -> None:
    lines = [json.dumps(record, separators=(",", ":")) for record in records]
    _write(root, "build-gates/cases.jsonl", "\n".join(lines))


def _scorer_active_ids(loaded: LoadedSet) -> tuple[str, ...]:
    """The scorer's active set as sorted ids, the loader's held equal to it."""

    files = tuple(loaded.cases_by_file.values())
    return tuple(sorted(str(case["id"]) for case in active_cases(files)))


class Refusals(unittest.TestCase):
    """Loader refusals name only content-free locations and fixes."""

    def assert_findings(self, root: Path, *expected: str) -> tuple[Finding, ...]:
        result = load_set(root)
        self.assertIsNone(result.loaded)
        rendered = "\n".join(finding.text() for finding in result.findings)
        for text in expected:
            self.assertIn(text, rendered)
        self.assertNotIn(SENTINEL, rendered)
        return result.findings

    def test_duplicate_id_names_both_case_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "build-gates/a.jsonl", _case("extraction-001"))
            _write(root, "build-gates/b.jsonl", _case("extraction-001"))
            _write(root, "slices/extraction/cases.ids", "extraction-001")
            findings = self.assert_findings(root, "a.jsonl", "b.jsonl", "extraction-001")
            self.assertEqual(len(findings), 1)

    def test_slice_id_resolving_to_no_case_names_the_id_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "build-gates/cases.jsonl", _case("extraction-001"))
            _write(root, "slices/extraction/cases.ids", "missing-case")
            findings = self.assert_findings(root, "cases.ids", "missing-case")
            self.assertEqual(len(findings), 1)

    def test_id_list_cannot_repeat_an_id_in_one_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "build-gates/cases.jsonl", _case("extraction-001"))
            _write(root, "slices/extraction/cases.ids", "extraction-001\nextraction-001")
            findings = self.assert_findings(root, "cases.ids", "listed twice")
            self.assertEqual(len(findings), 1)

    def test_slice_directory_requires_an_id_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "build-gates/cases.jsonl", _case("extraction-001"))
            (root / "slices" / "extraction").mkdir(parents=True)
            findings = self.assert_findings(root, "slices/extraction", "no id list")
            self.assertEqual(len(findings), 1)

    def test_non_json_and_missing_final_newline_findings_are_collected_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write(root, "suite/a.jsonl", "not one JSON object")
            _write(root, "build-gates/b.jsonl", _case("extraction-002"), final_newline=False)
            findings = self.assert_findings(root, "a.jsonl", "line 1", "b.jsonl", "final newline")
            locations = [(finding.file, finding.line or 0) for finding in findings]
            self.assertEqual(locations, sorted(locations))
            self.assertEqual(len(findings), 2)


class OrderAndProvenance(unittest.TestCase):
    """Filesystem creation order never changes the loaded set or its digest."""

    def _make_clean_set(self, root: Path, reverse: bool = False) -> None:
        files = (
            ("build-gates/z.jsonl", _case("extraction-002")),
            ("build-gates/a.jsonl", _case("extraction-001")),
            ("slices/z-slice/cases.ids", "extraction-002"),
            ("slices/a-slice/cases.ids", "extraction-001"),
        )
        for relative, text in reversed(files) if reverse else files:
            _write(root, relative, text)

    def test_sorted_file_and_slice_order_is_provenance_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first" / "eval-v9"
            second = Path(directory) / "second" / "eval-v9"
            self._make_clean_set(first)
            self._make_clean_set(second, reverse=True)
            left = load_set(first).loaded
            right = load_set(second).loaded
            self.assertIsNotNone(left)
            self.assertIsNotNone(right)
            assert left is not None and right is not None
            self.assertEqual(tuple(left.cases_by_file), ("build-gates/a.jsonl", "build-gates/z.jsonl"))
            self.assertEqual(tuple(left.slices), ("a-slice", "z-slice"))
            self.assertEqual(left.cases_by_file, right.cases_by_file)
            self.assertEqual(left.cases_by_id, right.cases_by_id)
            self.assertEqual(left.slices, right.slices)
            self.assertEqual(left.active_ids, right.active_ids)
            self.assertEqual(left.digest, right.digest)

    def test_findings_order_is_file_then_line_independent_of_creation_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first" / "eval-v9"
            second = Path(directory) / "second" / "eval-v9"
            _write(first, "build-gates/b.jsonl", "not JSON")
            _write(first, "build-gates/a.jsonl", "also not JSON")
            _write(second, "build-gates/a.jsonl", "also not JSON")
            _write(second, "build-gates/b.jsonl", "not JSON")
            first_result = load_set(first)
            second_result = load_set(second)
            first_findings = tuple(
                (finding.file, finding.line, finding.rule) for finding in first_result.findings
            )
            second_findings = tuple(
                (finding.file, finding.line, finding.rule) for finding in second_result.findings
            )
            self.assertEqual(first_findings, second_findings)

    def test_a_changed_case_byte_changes_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            self._make_clean_set(root)
            before = load_set(root).loaded
            self.assertIsNotNone(before)
            _write(root, "build-gates/a.jsonl", _case("extraction-001", "[FICTIONAL TEST QUESTION — changed]"))
            after = load_set(root).loaded
            self.assertIsNotNone(after)
            assert before is not None and after is not None
            self.assertNotEqual(before.digest, after.digest)

    def test_a_changed_id_list_byte_changes_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            self._make_clean_set(root)
            before = load_set(root).loaded
            self.assertIsNotNone(before)
            _write(root, "slices/a-slice/cases.ids", "extraction-001\nextraction-002")
            after = load_set(root).loaded
            self.assertIsNotNone(after)
            assert before is not None and after is not None
            self.assertNotEqual(before.digest, after.digest)
            self.assertNotEqual(before.slices, after.slices)

    def test_committed_set_loads_clean_in_whatever_tree_holds_it(self) -> None:
        court_map = load_court_map(ROOT / "courts.yaml").court_map
        assert court_map is not None
        result = load_set(ROOT / SET_ROOT, court_map.courts)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        harvest_absent = absent_from_export(SET_ROOT / "build-gates" / "extraction.jsonl", ROOT)
        self.assertEqual("build-gates/extraction.jsonl" in result.loaded.cases_by_file, not harvest_absent)
        extraction_ids = tuple(
            sorted(
                case["id"]
                for file, cases in result.loaded.cases_by_file.items()
                if file.startswith("build-gates/extraction")
                for case in cases
                if isinstance(case["id"], str)
            )
        )
        self.assertEqual(result.loaded.slices["extraction"], extraction_ids)


def _judgment_case(case_id: str = "judgments-001", question: str = SENTINEL) -> dict[str, object]:
    return {
        "id": case_id,
        "suite": "judgments",
        "category": "judgments",
        "branch": "legal",
        "question": question,
        "labels": ["chu-written", "rewritten"],
        "cluster_id": case_id,
        "notes": "",
        "review": {"by": "CSA-1", "on": "2026-09-19", "accepted_flags": []},
    }


def _superseding(case_id: str, target: str) -> str:
    record = json.loads(_case(case_id))
    notes = record.pop("notes")
    record["supersedes"] = target
    record["notes"] = notes
    return json.dumps(record, separators=(",", ":"))


class ShapeRefusals(unittest.TestCase):
    """The loader reports each shape and relationship rule without question text."""

    def _assert_case_finding(
        self,
        record: dict[str, object],
        *,
        court_ids: tuple[str, ...] | None = None,
        path: str = "build-gates/cases.jsonl",
        expected: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            suite_path = root / path
            _write(root, path, json.dumps(record, separators=(",", ":")))
            result = load_set(root, court_ids)
            self.assertIsNone(result.loaded)
            rendered = "\n".join(finding.text() for finding in result.findings)
            self.assertIn(suite_path.name, rendered)
            self.assertIn(str(record["id"]), rendered)
            self.assertIn(expected, rendered)
            self.assertNotIn(SENTINEL, rendered)

    def _assert_relational_finding(
        self,
        records: tuple[dict[str, object], ...],
        *,
        case_id: str,
        expected: str,
    ) -> None:
        """A relational rule needs the whole file, so the set is planted, not one case."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, records)
            result = load_set(root)
            self.assertIsNone(result.loaded)
            rendered = "\n".join(finding.text() for finding in result.findings)
            self.assertIn(f"cases.jsonl:id {case_id}", rendered)
            self.assertIn(expected, rendered)
            self.assertNotIn(SENTINEL, rendered)

    def test_version_directory_must_be_eval_vn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v-test"
            result = load_set(root)
            self.assertIsNone(result.loaded)
            self.assertTrue(any("eval-vN" in finding.rule for finding in result.findings))

    def test_shape_registry_rejects_unknown_shape_and_wrong_suite_directory(self) -> None:
        record = json.loads(_case("extraction-001"))
        record["category"] = "unknown"
        self._assert_case_finding(record, expected="unknown suite/category")
        self._assert_case_finding(
            json.loads(_case("extraction-001")), path="other/cases.jsonl", expected="directory"
        )

    def test_extraction_shape_rejects_keys_fixed_fields_and_id(self) -> None:
        cases = [
            ("case keys", lambda value: value.update(extra=True)),
            ("unknown suite/category", lambda value: value.update(suite="wrong")),
            ("unknown suite/category", lambda value: value.update(category="wrong")),
            ("branch", lambda value: value.update(branch="civil")),
            ("id pattern", lambda value: value.update(id="not-an-extraction-id")),
        ]
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                record = json.loads(_case("extraction-001"))
                mutate(record)
                self._assert_case_finding(record, expected=expected)

    def test_extraction_shape_rejects_question_labels_seed_cluster_review_notes_and_supersedes(self) -> None:
        cases: list[tuple[str, str, object]] = [
            ("question shape", "question", " " + SENTINEL),
            ("labels origin", "labels", ["wrong"]),
            ("harvest labels", "labels", ["harvest"]),
            ("seed", "seed", "bad"),
            ("cluster_id", "cluster_id", "bad"),
            ("review keys", "review", {"on": "2026-09-19", "by": "CSA-1"}),
            ("review.by", "review", {"by": "bad", "on": "2026-09-19"}),
            ("review.on", "review", {"by": "CSA-1", "on": "not-a-date"}),
            ("notes", "notes", None),
            ("supersedes string", "supersedes", 1),
        ]
        for expected, field, value in cases:
            with self.subTest(expected=expected):
                record = json.loads(_case("extraction-001"))
                record[field] = value
                self._assert_case_finding(record, expected=expected)

    def test_extraction_harvest_seed_and_cluster_rules(self) -> None:
        for field, value, expected in (
            ("seed", "HARV-nope", "seed"),
            ("cluster_id", "harvest-chat-NOT-LOWERCASE-HEX", "cluster_id"),
        ):
            with self.subTest(field=field):
                record = _harvest_extraction_case()
                record[field] = value
                self._assert_case_finding(record, expected=expected)
        record = json.loads(_case("extraction-001"))
        record["labels"] = ["invented", "doctrinal"]
        self._assert_case_finding(record, expected="invented labels")
        harvest = _harvest_extraction_case()
        review = harvest.pop("review")
        harvest["review"] = review
        self._assert_case_finding(harvest, expected="case keys or order")

    def test_variant_shape_rejects_key_order_labels_seed_and_invented_parent(self) -> None:
        # parent belongs between labels and cluster_id; re-inserting it puts it last.
        record = _variant_case()
        record["parent"] = record.pop("parent")
        self._assert_case_finding(record, expected="case keys or order")

        for labels in (["variant"], ["variant", "bad axis"]):
            with self.subTest(labels=labels):
                record = _variant_case()
                record["labels"] = labels
                self._assert_case_finding(record, expected="variant labels")

        record = _variant_case()
        record["seed"] = "HARV-001"
        self._assert_case_finding(record, expected="seed forbidden")

        record = _variant_case()
        record["parent"] = 1
        self._assert_case_finding(record, expected="parent")

        invented = json.loads(_case("extraction-001"))
        invented["parent"] = "extraction-000"
        self._assert_case_finding(invented, expected="case keys or order")

    def test_variant_parent_relationships_are_refused(self) -> None:
        self._assert_case_finding(
            _variant_case(parent="extraction-999"), expected="parent names no case"
        )

        self._assert_relational_finding(
            (
                _variant_case(case_id="extraction-001", parent="extraction-002"),
                json.loads(_case("extraction-002")),
            ),
            case_id="extraction-001",
            expected="parent names no earlier case of its id series",
        )
        self._assert_relational_finding(
            (
                json.loads(_case("extraction-001")),
                _variant_case(),
                _variant_case(case_id="extraction-003", parent="extraction-002"),
            ),
            case_id="extraction-003",
            expected="parent names a variant",
        )
        self._assert_relational_finding(
            (json.loads(_case("extraction-001")), _variant_case(cluster_id="other-cluster")),
            case_id="extraction-002",
            expected="cluster_id differs from the parent's",
        )

    def test_valid_variant_over_valid_parent_loads_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, (json.loads(_case("extraction-001")), _variant_case()))
            result = load_set(root)
            self.assertTrue(result.ok, result.findings)

    def test_active_ids_match_scorer_for_a_parent_chain(self) -> None:
        # A superseded parent, its variant, the successor, and the successor's variant.
        records = (
            json.loads(_case("extraction-001")),
            _variant_case(),
            json.loads(_superseding("extraction-003", "extraction-001")),
            _variant_case(case_id="extraction-004", parent="extraction-003"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "eval-v9"
            _write_cases(root, records)
            result = load_set(root)
            self.assertTrue(result.ok, result.findings)
            assert result.loaded is not None
            self.assertEqual(result.loaded.active_ids, ("extraction-003", "extraction-004"))
            self.assertEqual(result.loaded.active_ids, _scorer_active_ids(result.loaded))

    def test_committed_active_ids_match_scorer(self) -> None:
        court_map = load_court_map(ROOT / "courts.yaml").court_map
        assert court_map is not None
        result = load_set(ROOT / SET_ROOT, court_map.courts)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        self.assertEqual(result.loaded.active_ids, _scorer_active_ids(result.loaded))

    def test_judgment_shape_rejects_fields_and_review_contract(self) -> None:
        cases = [
            ("keys", lambda value: value.update(extra=True)),
            ("id pattern", lambda value: value.update(id="judgments-01")),
            ("labels.origin", lambda value: value.update(labels=["wrong", "rewritten"])),
            ("labels.wording", lambda value: value.update(labels=["chu-written", "wrong"])),
            ("seed forbidden", lambda value: value.update(seed="HARV-001")),
            ("cluster_id", lambda value: value.update(cluster_id="other")),
            ("notes", lambda value: value.update(notes=None)),
            ("review keys", lambda value: value.update(review={"by": "CSA-1", "on": "2026-09-19"})),
            ("review.accepted_flags", lambda value: value.update(review={"by": "CSA-1", "on": "2026-09-19", "accepted_flags": ["b", "a"]})),
            ("question shape", lambda value: value.update(question=" " + SENTINEL)),
        ]
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                record = _judgment_case()
                mutate(record)
                self._assert_case_finding(record, path="judgments/queries.jsonl", expected=expected)

    def test_judgment_fixed_fields_and_minimum_labels_are_checked(self) -> None:
        for field, value, expected in (
            ("suite", "wrong", "unknown suite/category"),
            ("category", "wrong", "unknown suite/category"),
            ("branch", "civil", "branch must be 'legal'"),
        ):
            with self.subTest(field=field):
                record = _judgment_case()
                record[field] = value
                self._assert_case_finding(record, path="judgments/queries.jsonl", expected=expected)
        record = _judgment_case()
        record["labels"] = ["chu-written"]
        self._assert_case_finding(record, path="judgments/queries.jsonl", expected="labels")

    def test_judgment_optional_dates_jurisdiction_and_supersedes_are_checked(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = [
            ("reference_date", {"reference_date": "bad"}),
            ("jurisdiction", {"jurisdiction": []}),
            ("supersedes string", {"supersedes": 1}),
        ]
        for expected, additions in cases:
            with self.subTest(expected=expected):
                record = _judgment_case()
                record.update(additions)
                self._assert_case_finding(record, path="judgments/queries.jsonl", expected=expected)

    def test_judgment_harvest_seed_cluster_and_query_type_rules(self) -> None:
        for field, value, expected in (
            ("seed", "HARV-nope", "seed"),
            ("cluster_id", "harvest-chat-bad space", "cluster_id"),
        ):
            with self.subTest(field=field):
                record: dict[str, object] = {
                    "id": "judgments-001",
                    "suite": "judgments",
                    "category": "judgments",
                    "branch": "legal",
                    "question": SENTINEL,
                    "labels": ["harvest", "rewritten", "doctrinal"],
                    "seed": "HARV-001",
                    "cluster_id": "harvest-chat-abcdef",
                    "notes": "",
                    "review": {"by": "CSA-1", "on": "2026-09-19", "accepted_flags": []},
                }
                record[field] = value
                self._assert_case_finding(record, path="judgments/queries.jsonl", expected=expected)
        query_type_record: dict[str, object] = {
            "id": "judgments-001",
            "suite": "judgments",
            "category": "judgments",
            "branch": "legal",
            "question": SENTINEL,
            "labels": ["harvest", "rewritten", "not-a-query-type"],
            "seed": "HARV-001",
            "cluster_id": "harvest-chat-abcdef",
            "notes": "",
            "review": {"by": "CSA-1", "on": "2026-09-19", "accepted_flags": []},
        }
        self._assert_case_finding(query_type_record, path="judgments/queries.jsonl", expected="labels.query_type")

    def test_judgment_harvest_optional_keys_have_the_declared_order(self) -> None:
        record = {
            "id": "judgments-001",
            "suite": "judgments",
            "category": "judgments",
            "branch": "legal",
            "question": SENTINEL,
            "reference_date": "2026-09-19",
            "jurisdiction": ["ca1"],
            "labels": ["harvest", "rewritten", "doctrinal"],
            "seed": "HARV-001",
            "cluster_id": "harvest-chat-abcdef",
            "notes": "",
            "review": {"by": "CSA-1", "on": "2026-09-19", "accepted_flags": []},
        }
        wrong_order = dict(record)
        jurisdiction = wrong_order.pop("jurisdiction")
        wrong_order["jurisdiction"] = jurisdiction
        self._assert_case_finding(
            {**wrong_order}, path="judgments/queries.jsonl", expected="case keys or order"
        )

    def test_judgment_question_cannot_contain_a_newline(self) -> None:
        record = _judgment_case(question=SENTINEL + "\ncontinued")
        self._assert_case_finding(record, path="judgments/queries.jsonl", expected="question shape")

    def test_jurisdiction_ids_must_be_in_the_handed_court_map(self) -> None:
        record = _judgment_case()
        record["jurisdiction"] = ["nd9"]
        self._assert_case_finding(
            record,
            court_ids=("ca1",),
            path="judgments/queries.jsonl",
            expected="absent from courts.yaml",
        )

    def test_supersedes_requires_an_existing_earlier_unique_target(self) -> None:
        cases = (
            ("names no case", "extraction-002", "extraction-009", ()),
            ("no earlier case", "extraction-001", "extraction-002", ()),
            ("already supersedes", "extraction-003", "extraction-001", ("extraction-002",)),
        )
        for expected, case_id, target, others in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "eval-v9"
                _write(root, "build-gates/a.jsonl", _superseding(case_id, target))
                lines = [_case("extraction-001"), _case("extraction-002")]
                lines = [line for line in lines if json.loads(line)["id"] != case_id]
                lines = [
                    _superseding(json.loads(line)["id"], target) if json.loads(line)["id"] in others else line
                    for line in lines
                ]
                _write(root, "build-gates/b.jsonl", "\n".join(lines))
                findings = [finding for finding in load_set(root).findings if expected in finding.rule]
                self.assertEqual([(finding.file, finding.id) for finding in findings], [("build-gates/a.jsonl", case_id)])

    def test_extraction_expected_objects_must_obey_the_contract(self) -> None:
        cases = [
            ("expected mapping", {"expected": []}),
            ("expected.objects list", {"expected": {"objects": {}}}),
            ("closed type", {"expected": {"objects": [{"type": "unknown"}]}}),
            (
                "keys or order",
                {"expected": {"objects": [{"start": 0, "type": "docket", "end": 1, "text": "["}]}},
            ),
            (
                "contract key invariant",
                {"expected": {"objects": [{"type": "statute", "start": 0, "end": 1, "text": "[", "subsections": []}]}},
            ),
            (
                "contract key invariant",
                {"expected": {"objects": [{"type": "docket", "start": 0, "end": 1, "text": "[", "key": "not-allowed"}]}},
            ),
            (
                "keys or order",
                {"expected": {"objects": [{"type": "bare_section", "start": 0, "end": 1, "text": "["}]}},
            ),
            (
                "keys or order",
                {"expected": {"objects": [{"type": "docket", "start": 0, "end": 1, "text": "[", "subsections": []}]}},
            ),
            (
                "field types",
                {"expected": {"objects": [{"type": "docket", "start": "0", "end": 1, "text": "["}]}},
            ),
            (
                "contract span invariant",
                {"expected": {"objects": [{"type": "docket", "start": 9, "end": 2, "text": "x"}]}},
            ),
            (
                "contract span invariant",
                {"expected": {"objects": [{"type": "docket", "start": 0, "end": 1, "text": "x"}]}},
            ),
            (
                "contract span invariant",
                {"expected": {"objects": [{"type": "docket", "start": 0, "end": 999, "text": SENTINEL}]}},
            ),
            (
                "contract key invariant",
                {"expected": {"objects": [{"type": "statute", "start": 0, "end": 1, "text": "["}]}},
            ),
            (
                "contract ordering invariant",
                {
                    "expected": {
                        "objects": [
                            {"type": "docket", "start": 0, "end": 1, "text": "["},
                            {"type": "docket", "start": 0, "end": 2, "text": "[F"},
                        ]
                    }
                },
            ),
        ]
        for expected, update in cases:
            with self.subTest(expected=expected):
                record = json.loads(_case("extraction-001"))
                record.update(update)
                self._assert_case_finding(record, expected=expected)


if __name__ == "__main__":
    unittest.main()
