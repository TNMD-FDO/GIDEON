"""Contracts for the committed synthetic cases and their category batch records."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Final, cast
from unittest import TestCase

from gideon.evaluation import synthetic
from gideon.evaluation.evalset import (
    HELD_OUT_SUFFIX,
    SET_ROOT,
    SHAPE_REGISTRY,
    load_set,
)
from gideon.host.corpus.lockfile import load_lockfile
from gideon.host.courts import load_court_map
from gideon.host.render.worker import (
    QUALIFIERS,
    SECTION_TYPES,
    TREATMENT_SIGNALS,
    TREATMENT_STATES,
)
from tools.exportboundary import is_excluded

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
README_PATH: Final[Path] = SET_ROOT / "research-qa/README.md"
REQUIRED_CATEGORIES: Final[frozenset[str]] = frozenset({
    "lookup", "retrieval", "synthesis", "existence", "treatment", "section-attribution",
    "unanswerable", "trap", "poisoned-premise",
})
PINNED_PREFIXES: Final[dict[str, tuple[tuple[int, str], ...]]] = {
    "lookup": (
        (30, "82ba7ecec6adcae4783f83b8776dcb8e0ddec767abeac513d4684fb88c16d3b8"),
    ),
    "retrieval": (
        (23, "66a7b26088fdfd340933434550640aa18b42624f2cb6f99cd736a907135fc521"),
        (78, "5e6860502f9f0ce182c17421f0958c49dde9474234a829d7fa684b07a0440c41"),
    ),
    "synthesis": (
        (48, "061ccb9ee3c7ebb85cd1e8436f2ce46fed0bc9017184633f21674bb261e93fac"),
    ),
    "existence": (
        (17, "3fb960f434f3c1cd3f4894b2d61449e79422698dbb27f76ebee6a16ade71291b"),
    ),
    "treatment": (
        (6, "3ab8d866637a8047cc7b791f4bd9b7f1fb79d0637235fce94aea216efc71420a"),
    ),
    "section-attribution": (
        (19, "178818c9820d235ecdf6041452ea8b4a9e49d4ed20e32dcf0453638892ac9e8a"),
    ),
    "unanswerable": (
        (26, "3f5156ee422bb0a13114741c7f778174af0c493b820beca880472efd5834baf2"),
    ),
    "trap": (
        (10, "8418643e8fde64cbf1983c6e445385e96a3b139aac02db1387da7b4e9e0cab34"),
    ),
    "poisoned-premise": (
        (8, "07d9a8813503eb84daca569c763f4bdd490cec503b4570a6edf2ee3ed81f0bb7"),
    ),
}


class SyntheticSetContract(TestCase):
    """The reviewed cases, prefix pins, and published counts agree."""

    def setUp(self) -> None:
        self.paths = {
            path.stem.removeprefix("synthetic-"): path.relative_to(ROOT)
            for path in sorted((ROOT / SET_ROOT / "research-qa").glob("synthetic-*.jsonl"))
        }
        self.case_data = {category: (ROOT / path).read_bytes() for category, path in self.paths.items()}
        self.cases = {
            category: tuple(cast(dict[str, object], json.loads(line)) for line in raw.splitlines())
            for category, raw in self.case_data.items()
        }

    def _batch_rows(self) -> tuple[dict[str, str], ...]:
        readme = (ROOT / README_PATH).read_text(encoding="utf-8")
        record = readme.split("### The pin and the batch record\n", 1)[1].split("\n## ", 1)[0]
        lines = [line for line in record.splitlines() if line.startswith("|")]
        self.assertGreaterEqual(len(lines), 3, "add the committed batch rows")
        columns = tuple(part.strip() for part in lines[0].strip("|").split("|"))
        self.assertTrue({"Category", "Accepted", "Closed-book-answerable", "Read", "Rejected"} <= set(columns))
        rows = []
        for line in lines[2:]:
            cells = tuple(part.strip().strip("`") for part in line.strip("|").split("|"))
            self.assertEqual(len(cells), len(columns))
            rows.append(dict(zip(columns, cells, strict=True)))
        return tuple(rows)

    def _court_ids(self) -> frozenset[str]:
        result = load_court_map(ROOT / "courts.yaml")
        self.assertTrue(result.ok, result.errors)
        assert result.court_map is not None
        return frozenset(result.court_map.courts)

    def test_every_committed_line_has_an_append_only_pin(self) -> None:
        self.assertTrue(self.paths.keys() >= REQUIRED_CATEGORIES)
        self.assertEqual(set(PINNED_PREFIXES), set(self.paths))
        for category, raw in self.case_data.items():
            with self.subTest(category=category):
                raw_lines = raw.splitlines(keepends=True)
                self.assertTrue(PINNED_PREFIXES[category], "add a committed prefix pin")
                previous = 0
                for count, expected_digest in PINNED_PREFIXES[category]:
                    with self.subTest(count=count):
                        self.assertGreater(count, previous)
                        self.assertLessEqual(count, len(raw_lines))
                        digest = hashlib.sha256(b"".join(raw_lines[:count])).hexdigest()
                        self.assertEqual(digest, expected_digest)
                        previous = count
                self.assertEqual(len(raw_lines), previous)
                self.assertTrue(raw_lines[-1].endswith(b"\n"))

    def test_ids_are_one_unbroken_series(self) -> None:
        for category, cases in self.cases.items():
            with self.subTest(category=category):
                self.assertEqual(
                    tuple(case["id"] for case in cases),
                    tuple(f"research-qa-{category}-{index:03d}" for index in range(1, len(cases) + 1)),
                )

    def test_template_and_labels_use_the_synthetic_vocabulary(self) -> None:
        for category, cases in self.cases.items():
            spec = SHAPE_REGISTRY[("research-qa", category)]
            for case in cases:
                with self.subTest(category=category, case_id=case["id"]):
                    self.assertEqual(case["category"], category)
                    template = cast(str, case["template"])
                    self.assertEqual(template.rsplit("@", 1)[0], category)
                    self.assertIn(template, synthetic.TEMPLATES)
                    if category == "lookup":
                        self.assertEqual(template, "lookup@1")
                    self.assertIn(case["labels"], (
                        [synthetic.SYNTHETIC_ORIGIN],
                        [synthetic.SYNTHETIC_ORIGIN, synthetic.CLOSED_BOOK_LABEL],
                    ))
                    expected = cast(dict[str, object], case["expected"])
                    self.assertEqual(tuple(expected), spec.expected_keys_for())
                    for key, vocabulary in (
                        ("state", (*TREATMENT_STATES, synthetic.NO_STATE)),
                        ("section_type", SECTION_TYPES),
                    ):
                        if key in spec.expected_keys_for():
                            self.assertIn(expected[key], vocabulary)
                    if category == "treatment":
                        self.assertEqual(template, "treatment@3")
                        relation = cast(dict[str, object], expected["relation"])
                        self.assertEqual(tuple(relation), synthetic.RELATION_KEYS)
                        for key, relation_vocabulary in (("standing", SECTION_TYPES), ("verb", TREATMENT_SIGNALS),
                                                         ("qualifier", QUALIFIERS)):
                            self.assertIn(relation[key], relation_vocabulary)
                        gold = cast(list[dict[str, object]], case["gold"])[0]
                        self.assertEqual(relation["citing"], gold["source_id"])
                        self.assertIs(type(relation["cited"]), int)
                        self.assertGreater(cast(int, relation["cited"]), 0)
                        self.assertEqual(relation["cited"], gold["cluster"])
                        actor = relation["actor"]
                        if isinstance(actor, str):
                            self.assertEqual(actor, gold["source_id"])
                        else:
                            self.assertIs(type(actor), int)
                            self.assertGreater(cast(int, actor), 0)
                            self.assertNotEqual(actor, gold["cluster"])
                    if category == "section-attribution":
                        self.assertEqual(template, "section-attribution@1")
                    if category in synthetic.NEAR_MISS_CATEGORIES:
                        self.assertEqual(expected["behavior"], synthetic.BEHAVIORS[category])
                        self.assertNotIn("must_cite", expected)
                        if category == "poisoned-premise":
                            premise = expected["premise"]
                            self.assertIsInstance(premise, str)
                            assert isinstance(premise, str)
                            self.assertTrue(premise)
                            self.assertEqual(premise, premise.strip())
                            self.assertEqual(premise.splitlines(), [premise])

    def test_gold_lengths_cites_and_clusters_agree(self) -> None:
        for category, cases in self.cases.items():
            spec = SHAPE_REGISTRY[("research-qa", category)]
            for case in cases:
                with self.subTest(category=category, case_id=case["id"]):
                    gold = cast(list[dict[str, object]], case["gold"])
                    self.assertTrue(gold)
                    expected = cast(dict[str, object], case["expected"])
                    self.assertEqual(case["cluster_id"], gold[0]["source_id"])
                    for index, entry in enumerate(gold, start=1):
                        with self.subTest(gold=index):
                            self.assertEqual(tuple(entry), spec.gold_keys_for())
                            self.assertEqual(
                                cast(int, entry["end"]) - cast(int, entry["start"]),
                                len(cast(str, entry["quote"])),
                            )
                            if "must_cite" in spec.expected_keys_for():
                                self.assertIn(entry["source_id"], cast(list[str], expected["must_cite"]))

    def test_jurisdiction_names_one_court_from_the_map(self) -> None:
        court_ids = self._court_ids()
        for category, cases in self.cases.items():
            for case in cases:
                with self.subTest(category=category, case_id=case["id"]):
                    jurisdiction = cast(list[str], case["jurisdiction"])
                    self.assertEqual(len(jurisdiction), 1)
                    self.assertIn(jurisdiction[0], court_ids)

    def test_every_review_names_a_csa_role(self) -> None:
        for category, cases in self.cases.items():
            spec = SHAPE_REGISTRY[("research-qa", category)]
            for case in cases:
                with self.subTest(category=category, case_id=case["id"]):
                    review = cast(dict[str, object], case["review"])
                    self.assertEqual(tuple(review), spec.review_keys_for(synthetic.SYNTHETIC_ORIGIN))
                    self.assertIsNotNone(re.fullmatch(r"CSA-[1-9][0-9]*", cast(str, review["by"])))
                    if not spec.zero_tolerance:
                        self.assertIsInstance(review["read"], bool)

    def test_asked_court_differs_from_the_gold_document_court(self) -> None:
        court_categories = {category for category in synthetic.NEAR_MISS_CATEGORIES
                            if "asked_court" in synthetic.TEMPLATES[f"{category}@1"].slots}
        corpus_courts: set[str] = set()
        for path in sorted((ROOT / "corpus/lockfiles").glob("*.yaml")):
            result = load_lockfile(path)
            self.assertTrue(result.ok, result.errors)
            assert result.lockfile is not None
            corpus_courts.update(result.lockfile.courts)
        self.assertTrue(corpus_courts)
        document_courts = {
            entry["source_id"]: cast(list[str], case["jurisdiction"])[0]
            for category, cases in self.cases.items() if category not in court_categories
            for case in cases for entry in cast(list[dict[str, object]], case["gold"])
        }
        for category in sorted(court_categories):
            for case in self.cases.get(category, ()):
                with self.subTest(category=category, case_id=case["id"]):
                    asked = cast(list[str], case["jurisdiction"])[0]
                    self.assertIn(asked, self._court_ids())
                    for gold in cast(list[dict[str, object]], case["gold"]):
                        if gold["source_id"] in document_courts:
                            self.assertNotEqual(asked, document_courts[gold["source_id"]])
                        else:
                            # The corpus cuts bound possible gold courts when no other case shares the document.
                            self.assertNotIn(asked, corpus_courts)

    def test_held_out_sidecars_resolve_in_their_file_and_reconcile_with_batch_rows(self) -> None:
        rows = self._batch_rows()
        research = ROOT / SET_ROOT / "research-qa"
        sidecars = sorted(research.glob(f"*{HELD_OUT_SUFFIX}"))
        self.assertTrue(sidecars)
        for sidecar in sidecars:
            with self.subTest(sidecar=sidecar.name):
                case_file = sidecar.with_name(sidecar.name.removesuffix(HELD_OUT_SUFFIX) + ".jsonl")
                self.assertTrue(case_file.is_file())
                cases = [json.loads(line) for line in case_file.read_bytes().splitlines()]
                ids = sidecar.read_text(encoding="utf-8").splitlines()
                self.assertTrue(set(ids) <= {case["id"] for case in cases})
                self.assertEqual(len(ids), len(set(ids)))
                categories = {case["category"] for case in cases}
                self.assertEqual(len(categories), 1)
                category = next(iter(categories))
                expected = sum(int(row["Held out"]) for row in rows if row["Category"] == category)
                for row in rows:
                    if row["Propagated"] == "none":
                        continue
                    for entry in row["Propagated"].split(", "):
                        name, count = entry.rsplit(" ", 1)
                        if name == category:
                            expected += int(count)
                self.assertEqual(len(ids), expected)

    def test_sampled_review_counts_match_the_batch_reads_minus_rejections(self) -> None:
        rows = self._batch_rows()
        for category, cases in self.cases.items():
            if SHAPE_REGISTRY[("research-qa", category)].zero_tolerance:
                continue
            with self.subTest(category=category):
                read = sum(cast(dict[str, object], case["review"])["read"] is True for case in cases)
                self.assertEqual(read, sum(
                    int(row["Read"]) - int(row["Rejected"])
                    for row in rows if row["Category"] == category
                ))

    def test_readme_counts_match_the_committed_cases(self) -> None:
        readme = (ROOT / README_PATH).read_text(encoding="utf-8")
        split = readme.split("## The split\n", 1)[1].split("\n## ", 1)[0]
        counts = re.findall(r"(?m)^\| `([a-z-]+)` \(synthetic\) \| ([0-9]+) \|$", split)
        self.assertEqual(len(counts), len(synthetic.PLANNED_COUNTS))
        self.assertEqual({category: int(count) for category, count in counts}, {
            category: len(self.cases.get(category, ())) for category in synthetic.PLANNED_COUNTS
        })
        rows = self._batch_rows()
        self.assertEqual({row["Category"] for row in rows}, set(synthetic.PLANNED_COUNTS))
        for category in synthetic.PLANNED_COUNTS:
            cases = self.cases.get(category, ())
            with self.subTest(category=category):
                batches = [row for row in rows if row["Category"] == category]
                for batch in batches:
                    self.assertGreaterEqual(int(batch["Accepted"]), 0)
                    self.assertGreaterEqual(int(batch["Closed-book-answerable"]), 0)
                    self.assertGreaterEqual(int(batch["Rejected"]), 0)
                    self.assertGreaterEqual(int(batch["Read"]), int(batch["Rejected"]))
                    self.assertEqual(int(batch["Accepted"]) + int(batch["Rejected"]), int(batch["Kept"]))
                    if SHAPE_REGISTRY[("research-qa", category)].zero_tolerance:
                        self.assertEqual(int(batch["Read"]), int(batch["Kept"]))
                self.assertEqual(sum(int(row["Accepted"]) for row in batches), len(cases))
                labelled = sum(synthetic.CLOSED_BOOK_LABEL in cast(list[str], case["labels"]) for case in cases)
                self.assertLessEqual(labelled, sum(int(row["Closed-book-answerable"]) for row in batches))
                if not cases:
                    self.assertFalse((ROOT / SET_ROOT / synthetic.case_path(category)).exists())

    def test_cases_are_present_and_load_in_an_exported_set(self) -> None:
        for category, path in self.paths.items():
            with self.subTest(category=category):
                self.assertFalse(is_excluded(path.as_posix()))
        court_ids = self._court_ids()
        with tempfile.TemporaryDirectory() as temporary:
            export_root = Path(temporary)
            for source in (ROOT / SET_ROOT).rglob("*"):
                relative = source.relative_to(ROOT)
                if source.is_file() and not is_excluded(relative.as_posix()):
                    destination = export_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
            result = load_set(export_root / SET_ROOT, court_ids)
            self.assertTrue(result.ok, result.findings)
            assert result.loaded is not None
            for category, path in self.paths.items():
                with self.subTest(category=category):
                    exported_cases = export_root / path
                    self.assertTrue(exported_cases.is_file())
                    self.assertEqual(exported_cases.read_bytes(), self.case_data[category])
                    self.assertEqual(
                        tuple(case["id"] for case in result.loaded.cases_by_file[path.relative_to(SET_ROOT).as_posix()]),
                        tuple(case["id"] for case in self.cases[category]),
                    )

    def test_no_slice_id_list_names_a_synthetic_case(self) -> None:
        paths = sorted((ROOT / SET_ROOT / "slices").rglob("*.ids"))
        self.assertTrue(paths)
        for category, cases in self.cases.items():
            case_ids = {cast(str, case["id"]) for case in cases}
            for path in paths:
                with self.subTest(category=category, path=path.relative_to(ROOT)):
                    listed = set(path.read_text(encoding="utf-8").splitlines())
                    self.assertFalse(case_ids & listed)
