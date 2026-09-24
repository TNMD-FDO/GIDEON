"""Contract tests for the pure court-map generator and its authoring command."""

from __future__ import annotations

import bz2
import csv
import hashlib
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from gideon.host.courts import (
    CIRCUITS,
    CourtMap,
    load_court_map,
    load_court_map_text,
    render_errors,
)
from tools.courtmap import generate, geography
from tools.courtmap.__main__ import main

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "courts"
CSV_PATH = FIXTURES / "courts-2099-01-02.csv.bz2"
GEOGRAPHY_PATH = FIXTURES / "geography.yaml"


def _fixture_rows() -> list[dict[str, object]]:
    with bz2.open(CSV_PATH, "rt", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _fixture_geography() -> geography.Geography:
    result = geography.load_geography(GEOGRAPHY_PATH)
    if not result.ok or result.geography is None:
        raise AssertionError(geography.render_errors(result.errors))
    return result.geography


ROWS = _fixture_rows()
GEOGRAPHY = _fixture_geography()
SOURCE_HASH = hashlib.sha256(CSV_PATH.read_bytes()).hexdigest()
SOURCE_DATE = CSV_PATH.name[7:17]


def _generate(
    rows: list[dict[str, object]] = ROWS,
    table: geography.Geography = GEOGRAPHY,
) -> CourtMap | tuple[generate.GenerationError, ...]:
    return generate.generate(
        rows,
        table,
        file=CSV_PATH.name,
        date=SOURCE_DATE,
        sha256=SOURCE_HASH,
    )


def _row_with_code(identifier: str, code: str) -> list[dict[str, object]]:
    rows = [dict(row) for row in ROWS]
    for row in rows:
        if row["id"] == identifier:
            row["jurisdiction"] = code
            break
    return rows


def _without_row(identifier: str) -> list[dict[str, object]]:
    return [dict(row) for row in ROWS if row["id"] != identifier]


class PureJoin(unittest.TestCase):
    """Exercise the CSV-to-geography join over visibly fictitious data."""

    def test_codes_federal_unplaced_and_corrections_map_to_levels(self) -> None:
        result = _generate()

        self.assertIsInstance(result, CourtMap)
        assert isinstance(result, CourtMap)
        expected = {
            "scotus": "scotus",
            "ca1": "circuit",
            "akd": "district",
            "fx-sup-al": "state_supreme",
            "fx-app-al": "state_appellate",
            "fx-ts-as": "state_supreme",
            "fx-app-gu": "state_appellate",
            "fx-unplaced": "other",
            "fx-correct-sup": "state_supreme",
            "fx-correct-other": "other",
        }
        for identifier, level in expected.items():
            with self.subTest(identifier=identifier):
                self.assertEqual(result.courts[identifier].level, level)

    def test_every_hand_table_refusal_names_its_section(self) -> None:
        cases = {
            "federal names court id 'fx-missing', but the CSV does not contain it": _generate(
                table=replace(GEOGRAPHY, federal={**GEOGRAPHY.federal, "fx-missing": None})
            ),
            "federal court 'scotus' has CSV code 'FD'; expected 'F'": _generate(
                _row_with_code("scotus", "FD")
            ),
            "state-level CSV row 'fx-app-al' is absent from state_courts and unplaced": _generate(
                table=replace(
                    GEOGRAPHY,
                    state_courts={k: v for k, v in GEOGRAPHY.state_courts.items() if k != "fx-app-al"},
                )
            ),
            "state_courts names court id 'fx-app-al', but the CSV does not contain it": _generate(
                _without_row("fx-app-al")
            ),
            "unplaced names court id 'fx-unplaced', but the CSV does not contain it": _generate(
                _without_row("fx-unplaced")
            ),
            "corrections names court id 'fx-correct-sup', but the CSV does not contain it": _generate(
                _without_row("fx-correct-sup")
            ),
            "state_courts row 'fx-correct-other' has corrected CSV code 'ST'": _generate(
                table=replace(GEOGRAPHY, state_courts={**GEOGRAPHY.state_courts, "fx-correct-other": "AL"})
            ),
            "correction for 'fx-correct-sup' does not change CSV code 'SA'": _generate(
                table=replace(
                    GEOGRAPHY,
                    corrections={**GEOGRAPHY.corrections, "fx-correct-sup": geography.Correction("SA", "unchanged")},
                )
            ),
        }
        for problem, result in cases.items():
            with self.subTest(problem=problem):
                self.assertIsInstance(result, tuple)
                assert isinstance(result, tuple)
                self.assertTrue(any(problem in error.problem for error in result), result)
                self.assertTrue(all("tools/courtmap/geography.yaml section" in error.fix for error in result))

    def test_empty_id_and_name_are_refused(self) -> None:
        empty_id: list[dict[str, object]] = [
            *ROWS,
            {"id": "", "jurisdiction": "S", "full_name": "Fixture"},
        ]
        empty_name: list[dict[str, object]] = [
            *ROWS,
            {"id": "fx-empty-name", "jurisdiction": "S", "full_name": ""},
        ]

        for rows, text in ((empty_id, "empty court id"), (empty_name, "empty full_name")):
            with self.subTest(text=text):
                result = _generate(rows)
                self.assertIsInstance(result, tuple)
                assert isinstance(result, tuple)
                self.assertTrue(any(text in error.problem for error in result))

    def test_law_counts_and_state_supreme_coverage_are_refused(self) -> None:
        no_scotus = _without_row("scotus")
        no_scotus_table = replace(
            GEOGRAPHY,
            federal={key: value for key, value in GEOGRAPHY.federal.items() if key != "scotus"},
        )
        count_result = _generate(no_scotus, no_scotus_table)
        self.assertIsInstance(count_result, tuple)
        assert isinstance(count_result, tuple)
        self.assertTrue(any("law-fixed scotus count" in error.problem for error in count_result))

        no_state = _without_row("fx-sup-ak")
        no_state_table = replace(
            GEOGRAPHY,
            state_courts={key: value for key, value in GEOGRAPHY.state_courts.items() if key != "fx-sup-ak"},
        )
        state_result = _generate(no_state, no_state_table)
        self.assertIsInstance(state_result, tuple)
        assert isinstance(state_result, tuple)
        self.assertTrue(any("state 'AK' has no state_supreme court" in error.problem for error in state_result))

    def test_writer_is_byte_stable_and_round_trips_through_host_loader(self) -> None:
        result = _generate()

        self.assertIsInstance(result, CourtMap)
        assert isinstance(result, CourtMap)
        first = generate.render_court_map(result)
        self.assertEqual(first, generate.render_court_map(result))
        loaded = load_court_map_text(first)
        self.assertTrue(loaded.ok, "\n".join(error.problem for error in loaded.errors))
        self.assertEqual(loaded.court_map, result)

    def test_hand_table_strict_loader_catches_shape_and_placement_errors(self) -> None:
        source = GEOGRAPHY_PATH.read_text(encoding="utf-8")
        cases = {
            "duplicate id": source.replace(
                "states:\n", "states:\n  AL: {name: Duplicate, circuit: ca11}\n", 1
            ),
            "sixth section": source + "\nsixth: {}\n",
            "unknown entry key": source.replace(
                "  AL: {name: Fixture AL, circuit: ca11}",
                "  AL:\n    name: Fixture AL\n    circuit: ca11\n    circut: ca11",
                1,
            ),
            "id in two placements": source.replace(
                "state_courts:\n", "state_courts:\n  fx-unplaced: AL\n", 1
            ),
            "null state name": source.replace(
                "  AL: {name: Fixture AL, circuit: ca11}",
                "  AL: {name: null, circuit: ca11}",
                1,
            ),
            "null correction reason": source.replace(
                "reason: \"fixture correction onto state supreme\"",
                "reason: null",
                1,
            ),
            "correction outside CSV_CODES": source.replace(
                "code: S, reason: \"fixture correction onto state supreme\"",
                "code: NOPE, reason: \"fixture correction onto state supreme\"",
                1,
            ),
        }
        expected = {
            "duplicate id": "duplicate mapping key 'AL'",
            "sixth section": "Unknown key 'sixth'",
            "unknown entry key": "nearest valid key is 'circuit'",
            "id in two placements": "'fx-unplaced' appears in both state_courts and unplaced",
            "null state name": "'states.AL.name': expected a non-empty string (got None)",
            "null correction reason": (
                "'corrections.fx-correct-sup.reason': expected a non-empty string (got None)"
            ),
            "correction outside CSV_CODES": "expected a CourtListener jurisdiction code",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "geography.yaml"
            for label, text in cases.items():
                with self.subTest(label=label):
                    path.write_text(text, encoding="utf-8")
                    result = geography.load_geography(path)
                    self.assertFalse(result.ok)
                    self.assertTrue(result.errors)
                    self.assertTrue(all("tools/courtmap/geography.yaml" in error.fix for error in result.errors))
                    self.assertTrue(
                        any(expected[label] in error.problem for error in result.errors),
                        geography.render_errors(result.errors),
                    )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "geography.yaml"
            path.write_text(source, encoding="utf-8")
            result = geography.load_geography(path)
            self.assertTrue(result.ok, geography.render_errors(result.errors))
            assert result.geography is not None
            self.assertIn("fx-correct-sup", result.geography.corrections)
            self.assertIn("fx-correct-sup", result.geography.state_courts)


class Command(unittest.TestCase):
    """Exercise the local-only authoring command's dates, hash, and check mode."""

    def _checkout(self, directory: str) -> Path:
        root = Path(directory)
        target = root / "tools" / "courtmap"
        target.mkdir(parents=True)
        shutil.copyfile(GEOGRAPHY_PATH, target / "geography.yaml")
        return root

    def test_command_records_date_and_hash_and_check_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._checkout(directory)
            self.assertEqual(main(["--csv", str(CSV_PATH)], root=root), 0)
            output = root / "courts.yaml"
            loaded = load_court_map_text(output.read_text(encoding="utf-8"))
            self.assertTrue(loaded.ok)
            assert loaded.court_map is not None
            self.assertEqual(loaded.court_map.source.file, CSV_PATH.name)
            self.assertEqual(loaded.court_map.source.date, SOURCE_DATE)
            self.assertEqual(loaded.court_map.source.sha256, SOURCE_HASH)
            self.assertEqual(main(["--csv", str(CSV_PATH), "--check"], root=root), 0)
            output.write_text(output.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
            self.assertEqual(main(["--csv", str(CSV_PATH), "--check"], root=root), 1)

    def test_command_refuses_a_non_dated_source_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._checkout(directory)
            bad_path = root / "not-a-courts-file.csv.bz2"
            shutil.copyfile(CSV_PATH, bad_path)

            self.assertEqual(main(["--csv", str(bad_path)], root=root), 1)
            self.assertFalse((root / "courts.yaml").exists())


class CommittedPair(unittest.TestCase):
    """The committed table and map agree, though CI never sees the CSV."""

    table: geography.Geography
    court_map: CourtMap

    @classmethod
    def setUpClass(cls) -> None:
        table = geography.load_geography(ROOT / "tools" / "courtmap" / "geography.yaml")
        loaded = load_court_map(ROOT / "courts.yaml")
        if not table.ok or table.geography is None:
            raise AssertionError(geography.render_errors(table.errors))
        if not loaded.ok or loaded.court_map is None:
            raise AssertionError(render_errors(loaded.errors))
        cls.table = table.geography
        cls.court_map = loaded.court_map

    def _level(self, identifier: str) -> str:
        court = self.court_map.court(identifier)
        self.assertIsNotNone(court, identifier)
        assert court is not None
        return court.level

    def test_state_courts_sit_at_a_state_level_in_their_state(self) -> None:
        for identifier, state in self.table.state_courts.items():
            with self.subTest(identifier=identifier):
                self.assertIn(self._level(identifier), ("state_supreme", "state_appellate"))
                self.assertEqual(self.court_map.courts[identifier].state, state)

    def test_federal_ids_sit_at_their_level(self) -> None:
        for identifier, state in self.table.federal.items():
            with self.subTest(identifier=identifier):
                expected = (
                    "scotus" if identifier == "scotus"
                    else "circuit" if identifier in CIRCUITS
                    else "district"
                )
                self.assertEqual(self._level(identifier), expected)
                self.assertEqual(self.court_map.courts[identifier].state, state)

    def test_unplaced_ids_sit_at_other(self) -> None:
        for identifier in self.table.unplaced:
            with self.subTest(identifier=identifier):
                self.assertEqual(self._level(identifier), "other")

    def test_corrected_ids_sit_at_their_corrected_codes_level(self) -> None:
        for identifier, correction in self.table.corrections.items():
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    self._level(identifier),
                    geography.LEVEL_BY_CODE.get(correction.code, "other"),
                )


if __name__ == "__main__":
    unittest.main()
