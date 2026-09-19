"""Contract tests for the committed court map and its host loader."""

from __future__ import annotations

import unittest
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from gideon.host.courts import (
    CIRCUITS,
    LEVELS,
    STATE_CODES,
    STATES,
    CourtMap,
    load_court_map,
    render_errors,
)

ROOT = Path(__file__).resolve().parent.parent
COURT_MAP_PATH = ROOT / "courts.yaml"
COURT_FIXTURES = ROOT / "tests" / "fixtures" / "courts"
SITE_PATHS = (ROOT / "config", ROOT / "tests" / "fixtures" / "site")
MINIMUM_SITE_FILES = 2

COURT_MAP_RESULT = load_court_map(COURT_MAP_PATH)
COURT_MAP = COURT_MAP_RESULT.court_map


def _lock_lists(value: object, path: str = "") -> list[tuple[str, list[object]]]:
    found: list[tuple[str, list[object]]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key == "courts":
                if not isinstance(child, list):
                    raise AssertionError(f"{child_path} must be a list")
                found.append((child_path, child))
            found.extend(_lock_lists(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_lock_lists(child, f"{path}[{index}]"))
    return found


def _assert_lockfile_directory(path: Path, court_map: CourtMap) -> None:
    if not path.is_dir():
        return
    for lockfile in sorted(path.rglob("*.yaml")):
        _assert_lockfile(lockfile, court_map)


def _assert_lockfile(path: Path, court_map: CourtMap) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    lists = _lock_lists(document)
    if not lists:
        raise AssertionError(f"{path} has no courts key")
    for key_path, identifiers in lists:
        unknown = court_map.unresolved(str(identifier) for identifier in identifiers)
        if unknown:
            raise AssertionError(f"{path} {key_path} has unknown courts: {unknown}")


class CourtMapContract(unittest.TestCase):
    """Exercise §8.6's committed artifact contract without a CSV fixture."""

    def test_committed_map_loads_and_has_closed_lists_and_statute_counts(self) -> None:
        self.assertTrue(COURT_MAP_RESULT.ok, render_errors(COURT_MAP_RESULT.errors))
        self.assertIsNotNone(COURT_MAP)
        assert COURT_MAP is not None
        self.assertTrue(
            all(court.level in LEVELS for court in COURT_MAP.courts.values())
        )
        self.assertTrue(
            all(
                court.circuit is None or court.circuit in CIRCUITS
                for court in COURT_MAP.courts.values()
            )
        )
        self.assertTrue(
            all(
                court.state is None or court.state in STATE_CODES
                for court in COURT_MAP.courts.values()
            )
        )
        # The statute's counts: 28 U.S.C. §§ 41 (13 circuits) and 81–131 (94 districts).
        counts = Counter(court.level for court in COURT_MAP.courts.values())
        self.assertEqual(counts["scotus"], 1)
        self.assertEqual(counts["circuit"], 13)
        self.assertEqual(counts["district"], 94)
        for state in STATES:
            with self.subTest(state=state):
                self.assertTrue(
                    any(
                        court.state == state and court.level == "state_supreme"
                        for court in COURT_MAP.courts.values()
                    )
                )

    def test_source_counts_equal_the_court_body(self) -> None:
        self.assertIsNotNone(COURT_MAP)
        assert COURT_MAP is not None
        counts = Counter(court.level for court in COURT_MAP.courts.values())
        expected_levels = {level: counts[level] for level in LEVELS}
        self.assertEqual(COURT_MAP.source.rows, len(COURT_MAP.courts))
        self.assertEqual(dict(COURT_MAP.source.levels), expected_levels)

    def test_queries_cover_levels_nearest_unresolved_and_state_expansion(self) -> None:
        self.assertIsNotNone(COURT_MAP)
        assert COURT_MAP is not None
        district_id = COURT_MAP.ids_at_level("district")[0]
        district = COURT_MAP.court(district_id)
        self.assertIsNotNone(district)
        assert district is not None
        self.assertEqual(district.id, district_id)
        self.assertEqual(
            COURT_MAP.ids_at_level("circuit"),
            tuple(sorted(COURT_MAP.ids_at_level("circuit"))),
        )
        self.assertIn(
            COURT_MAP.nearest_id(district_id + "x", "district"),
            COURT_MAP.ids_at_level("district"),
        )
        self.assertEqual(
            COURT_MAP.unresolved((district_id, "fixture-unknown")),
            ("fixture-unknown",),
        )
        state = next(
            court.state
            for court in COURT_MAP.courts.values()
            if court.level == "state_supreme" and court.state is not None
        )
        appellate = COURT_MAP.appellate_courts(state)
        self.assertTrue(appellate)
        self.assertEqual(
            {COURT_MAP.courts[identifier].state for identifier in appellate},
            {state},
        )

    def test_loader_malformed_fixtures_collect_errors_with_restore_fix(self) -> None:
        malformed = sorted((COURT_FIXTURES / "maps").glob("*.yaml"))
        self.assertGreaterEqual(len(malformed), 2)
        for path in malformed:
            with self.subTest(path=path.name):
                result = load_court_map(path)
                self.assertFalse(result.ok)
                self.assertTrue(result.errors)
                self.assertTrue(all("Restore courts.yaml from the release checkout" in error.fix for error in result.errors))
                self.assertTrue(all("docs/archi/host.md" in error.fix for error in result.errors))
        collected = load_court_map(COURT_FIXTURES / "maps" / "collect-all.yaml")
        paths = {error.key_path for error in collected.errors}
        self.assertIn("unexpected", paths)
        self.assertIn("source.sha256", paths)
        self.assertIn("source.date", paths)
        self.assertIn("source.levels.stray", paths)
        self.assertIn("courts.fixture.extra", paths)

    def test_a_present_null_in_a_required_leaf_is_refused(self) -> None:
        result = load_court_map(COURT_FIXTURES / "maps" / "null-values.yaml")

        self.assertFalse(result.ok)
        refused = {error.key_path: error.problem for error in result.errors}
        for key_path in (
            "source.file",
            "source.date",
            "source.sha256",
            "source.rows",
            "courts.fixture.level",
            "courts.fixture.name",
        ):
            with self.subTest(key_path=key_path):
                self.assertIn("(got None)", refused.get(key_path, ""))
        self.assertNotIn("courts.fixture.circuit", refused)
        self.assertNotIn("courts.fixture.state", refused)

    def test_duplicate_id_is_a_loader_refusal(self) -> None:
        result = load_court_map(COURT_FIXTURES / "maps" / "duplicate-id.yaml")

        self.assertFalse(result.ok)
        self.assertIn("duplicate mapping key", result.errors[0].problem)
        self.assertTrue(result.errors[0].fix.startswith("Restore courts.yaml"))

    def test_missing_file_and_missing_courts_key_have_release_restore_fix(self) -> None:
        cases = {
            "court map is missing": COURT_FIXTURES / "maps" / "absent.yaml",
            "Invalid value for 'courts': missing required key": COURT_FIXTURES / "maps" / "no-courts-key.yaml",
        }
        for problem, path in cases.items():
            with self.subTest(problem=problem):
                result = load_court_map(path)
                self.assertFalse(result.ok)
                self.assertIn(problem, result.errors[0].problem)
                self.assertIn("release checkout", result.errors[0].fix)

    def test_site_files_resolve_at_their_leaf_levels(self) -> None:
        self.assertIsNotNone(COURT_MAP)
        assert COURT_MAP is not None
        paths = sorted(path for root in SITE_PATHS for path in root.rglob("*.yaml"))
        self.assertGreaterEqual(len(paths), MINIMUM_SITE_FILES)
        visited = 0
        for path in paths:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(document, Mapping) or not isinstance(document.get("jurisdiction"), Mapping):
                continue
            visited += 1
            jurisdiction = document["jurisdiction"]
            assert isinstance(jurisdiction, Mapping)
            circuit = jurisdiction.get("circuit")
            if isinstance(circuit, str):
                court = COURT_MAP.court(circuit)
                self.assertIsNotNone(court)
                assert court is not None
                self.assertEqual(court.level, "circuit")
            for identifier in jurisdiction.get("districts", []):
                court = COURT_MAP.court(identifier)
                self.assertIsNotNone(court)
                assert court is not None
                self.assertEqual(court.level, "district")
            for identifier in jurisdiction.get("states", []):
                court = COURT_MAP.court(identifier)
                self.assertIsNotNone(court)
                assert court is not None
                self.assertEqual(court.level, "state_supreme")
        self.assertGreaterEqual(visited, MINIMUM_SITE_FILES)

    def test_lockfile_assertion_is_green_without_directory_and_bites_on_fixtures(self) -> None:
        self.assertIsNotNone(COURT_MAP)
        assert COURT_MAP is not None
        _assert_lockfile_directory(ROOT / "corpus" / "lockfiles", COURT_MAP)
        fixture_dir = COURT_FIXTURES / "lockfiles"
        _assert_lockfile(fixture_dir / "resolves.yaml", COURT_MAP)
        with self.assertRaisesRegex(AssertionError, "unknown courts: \\('fixture-unknown',\\)"):
            _assert_lockfile(fixture_dir / "unknown-id.yaml", COURT_MAP)
        with self.assertRaisesRegex(AssertionError, "has no courts key"):
            _assert_lockfile(fixture_dir / "no-courts.yaml", COURT_MAP)


if __name__ == "__main__":
    unittest.main()
