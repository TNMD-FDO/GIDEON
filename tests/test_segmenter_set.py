"""Contracts for the frozen segmenter sample and its draw record."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Final, cast
from unittest import TestCase

from gideon.evaluation.evalset import (
    SEGMENTER_TEXT_SOURCES,
    SET_ROOT,
    load_set,
    select_cases,
)
from gideon.evaluation.segmenter_labels import LABELS_PATH
from gideon.host.courts import load_court_map
from tools.exportboundary import is_excluded

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CASES_PATH: Final[Path] = SET_ROOT / "build-gates/segmenter-300.jsonl"
IDS_PATH: Final[Path] = SET_ROOT / "slices/segmenter-300/sample.ids"
MIRROR_IDS_PATH: Final[Path] = SET_ROOT / "slices/build-gates/sample.ids"
LABELS_FILE: Final[Path] = SET_ROOT / LABELS_PATH
README_PATH: Final[Path] = SET_ROOT / "build-gates/README.md"
DRAWN: Final[int] = 300
PINNED_PREFIXES: Final[dict[Path, tuple[tuple[int, str], ...]]] = {
    CASES_PATH: (
        (300, "bd2c6c54d8185455032374d3c8cad789f09f6697d649855f6d5647efea01cc09"),
        (306, "e660b1121647d95eae755fde87f32f08af1d7fc93f9cbd22ba2690bb6555057a"),
    ),
    IDS_PATH: (
        (300, "7fd74a3af7177e6b300370ce73f913a60a73620693f0dcc9de864a08feacb665"),
        (306, "436b12aa4887bc7ed1bc897a79cb23af5a5ec42016d40c0b9e599070c79e7969"),
    ),
    LABELS_FILE: (
        (298, "7fc76234e79112175e0fc7cb92684658338ae8578cd0818bb5a2f32a297842ab"),
        (304, "91dac15b05def95b8b364d52ca111cd983ae3e87c2ad60aeb2353e1913d58f38"),
    ),
}
PINNED_PREFIXES[MIRROR_IDS_PATH] = PINNED_PREFIXES[IDS_PATH]
COURTS: Final[frozenset[str]] = frozenset({"ca6", "scotus"})
CELL: Final[re.Pattern[str]] = re.compile(r"([1-9][0-9]*) / ([1-9][0-9]*)")


def _cases() -> tuple[dict[str, object], ...]:
    return tuple(
        cast(dict[str, object], json.loads(line))
        for line in (ROOT / CASES_PATH).read_bytes().splitlines()
    )


def _draw_and_successors() -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    cases = _cases()
    return cases[:DRAWN], cases[DRAWN:]


def _readme_retirements() -> tuple[tuple[str, str, int, str, str, int], ...]:
    readme = (ROOT / README_PATH).read_text(encoding="utf-8")
    if "## Retirements\n" not in readme:
        return ()
    section = readme.split("## Retirements\n", 1)[1].split("\n## ", 1)[0]
    lines = [line for line in section.splitlines() if line.startswith("|")]
    if not lines or lines[0] != "| Retired | Successor | Opinion | Court | Source | Decade | Why |":
        raise ValueError("retirements table header")
    rows: list[tuple[str, str, int, str, str, int]] = []
    for line in lines[1:]:
        parts = tuple(part.strip().strip("`") for part in line.strip("|").split("|"))
        if all(re.fullmatch(r":?-+:?", part) for part in parts):
            continue
        if len(parts) != 7 or not all(parts):
            raise ValueError("retirements table row")
        retired, successor, opinion, court, source, decade, _why = parts
        rows.append((retired, successor, int(opinion), court, source, int(decade.removesuffix("s"))))
    return tuple(rows)


def _readme_tables() -> tuple[
    dict[tuple[str, str, int], tuple[int, int]], dict[tuple[str, str], tuple[int, int]]
]:
    section = (
        (ROOT / README_PATH).read_text(encoding="utf-8").split("## The draw\n", 1)[1]
    )
    section = section.split("## The labels file\n", 1)[0]
    cells: dict[tuple[str, str, int], tuple[int, int]] = {}
    totals: dict[tuple[str, str], tuple[int, int]] = {}
    court: str | None = None
    sources: tuple[str, ...] = ()
    for line in section.splitlines():
        heading = re.fullmatch(r"\*\*([a-z0-9]+)\*\*", line)
        if heading is not None:
            court = heading.group(1)
            sources = ()
        elif line.startswith("| Decade |"):
            sources = tuple(
                part.strip().strip("`") for part in line.strip("|").split("|")[1:]
            )
        elif line.startswith("| ") and not line.startswith("|---"):
            if court is None or not sources:
                raise ValueError("draw table has no court or source heading")
            parts = tuple(part.strip() for part in line.strip("|").split("|"))
            if len(parts) != len(sources) + 1:
                raise ValueError("draw table row width")
            decade_text = parts[0]
            if (
                decade_text != "total"
                and re.fullmatch(r"[0-9]{4}s", decade_text) is None
            ):
                raise ValueError("draw table decade")
            for source, value in zip(sources, parts[1:], strict=True):
                if not value:
                    continue
                pair = CELL.fullmatch(value)
                if pair is None:
                    raise ValueError("draw table cell")
                counts = (int(pair.group(1)), int(pair.group(2)))
                if decade_text == "total":
                    key = (court, source)
                    if key in totals:
                        raise ValueError("duplicate draw total")
                    totals[key] = counts
                else:
                    cell_key = (court, source, int(decade_text[:-1]))
                    if cell_key in cells:
                        raise ValueError("duplicate draw cell")
                    cells[cell_key] = counts
    return cells, totals


class SegmenterSetContract(TestCase):
    """The sample, its ids, and the published draw table agree."""

    def test_every_existing_file_has_an_append_only_pin(self) -> None:
        for path, prefixes in PINNED_PREFIXES.items():
            with self.subTest(path=path):
                full_path = ROOT / path
                if path == LABELS_FILE and not full_path.exists():
                    continue
                raw_lines = full_path.read_bytes().splitlines(keepends=True)
                self.assertTrue(prefixes, f"{path}: add a prefix pin")
                previous = 0
                for count, expected_digest in prefixes:
                    self.assertGreater(count, previous)
                    self.assertLessEqual(count, len(raw_lines))
                    actual = hashlib.sha256(b"".join(raw_lines[:count])).hexdigest()
                    self.assertEqual(actual, expected_digest)
                    previous = count
                self.assertEqual(len(raw_lines), previous)
                self.assertTrue(raw_lines[-1].endswith(b"\n"))

    def test_ids_and_strata_are_ordered_and_known(self) -> None:
        drawn, successors = _draw_and_successors()
        cases = drawn + successors
        ids = tuple(case["id"] for case in cases)
        self.assertEqual(
            tuple(case["id"] for case in drawn),
            tuple(f"segmenter-{index:03d}" for index in range(1, DRAWN + 1)),
        )
        self.assertEqual(len(drawn), DRAWN)
        court_result = load_court_map(ROOT / "courts.yaml")
        self.assertTrue(court_result.ok, court_result.errors)
        assert court_result.court_map is not None
        court_ids = court_result.court_map.courts
        for case in cases:
            with self.subTest(case_id=case["id"]):
                self.assertIn(case["court"], court_ids)
                self.assertIn(case["text_source"], SEGMENTER_TEXT_SOURCES)
                self.assertIs(type(case["decade"]), int)
                self.assertEqual(cast(int, case["decade"]) % 10, 0)
                self.assertIs(type(case["opinion_id"]), int)
                self.assertGreater(cast(int, case["opinion_id"]), 0)
        order = [
            cast(tuple[str, str, int, int], (
                case["court"], case["text_source"], case["decade"], case["opinion_id"],
            )) for case in drawn
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual({case["court"] for case in drawn}, COURTS)

        earlier = {case["id"]: case for case in drawn}
        retired_ids: set[str] = set()
        recorded_cells = {retired: (court, source, decade) for retired, _, _, court, source, decade in _readme_retirements()}
        for ordinal, case in enumerate(successors, DRAWN + 1):
            with self.subTest(successor=case["id"]):
                self.assertEqual(case["id"], f"segmenter-{ordinal:03d}")
                retired = cast(str, case["supersedes"])
                self.assertIn(retired, earlier)
                self.assertNotIn(retired, retired_ids)
                original = earlier[retired]
                cell = (case["court"], case["text_source"], case["decade"])
                if cell != (original["court"], original["text_source"], original["decade"]):
                    self.assertIn(retired, recorded_cells)
                    self.assertEqual(cell, recorded_cells[retired])
                    self.assertEqual(cell[:2], (original["court"], original["text_source"]))
                retired_ids.add(retired)
                earlier[case["id"]] = case

        for path in (IDS_PATH, MIRROR_IDS_PATH):
            with self.subTest(path=path):
                ids_text = (ROOT / path).read_text(encoding="utf-8")
                self.assertTrue(ids_text.endswith("\n"))
                self.assertEqual(tuple(ids_text.splitlines()), ids)
        result = load_set(ROOT / SET_ROOT, court_ids)
        self.assertTrue(result.ok, result.findings)
        assert result.loaded is not None
        self.assertEqual(sum(
            result.loaded.cases_by_id[case_id].get("category") == "segmenter-300"
            for case_id in result.loaded.active_ids
        ), DRAWN)
        selection = select_cases(result.loaded, "segmenter-300")
        self.assertEqual(len(selection.counted) + len(selection.unsigned), DRAWN)

    def test_readme_retirements_match_successors(self) -> None:
        _, successors = _draw_and_successors()
        self.assertEqual(_readme_retirements(), tuple(
            (case["supersedes"], case["id"], case["opinion_id"], case["court"], case["text_source"], case["decade"])
            for case in successors
        ))

    def test_readme_draw_tables_match_every_case(self) -> None:
        readme = (ROOT / README_PATH).read_text(encoding="utf-8")
        self.assertRegex(readme, r"(?m)^The seed is `20261008`, 150 per court,")
        cells, totals = _readme_tables()
        self.assertEqual({court for court, _, _ in cells}, COURTS)
        drawn = Counter(
            (
                cast(str, case["court"]),
                cast(str, case["text_source"]),
                cast(int, case["decade"]),
            )
            for case in _draw_and_successors()[0]
        )
        self.assertEqual({key: count for key, (_, count) in cells.items()}, dict(drawn))
        by_source: Counter[tuple[str, str]] = Counter()
        population_by_source: Counter[tuple[str, str]] = Counter()
        for (court, source, _decade), (population, count) in cells.items():
            self.assertIn(source, SEGMENTER_TEXT_SOURCES)
            self.assertLessEqual(count, population)
            self.assertGreaterEqual(count, 1)
            by_source[(court, source)] += count
            population_by_source[(court, source)] += population
        self.assertEqual(set(totals), set(by_source))
        for key, (population, count) in totals.items():
            self.assertEqual(
                (population, count), (population_by_source[key], by_source[key])
            )
        for court in COURTS:
            self.assertEqual(
                sum(count for (name, _), count in by_source.items() if name == court),
                150,
            )

    def test_set_loads_in_development_and_export(self) -> None:
        court_result = load_court_map(ROOT / "courts.yaml")
        self.assertTrue(court_result.ok, court_result.errors)
        assert court_result.court_map is not None
        result = load_set(ROOT / SET_ROOT, court_result.court_map.courts)
        self.assertTrue(result.ok, result.findings)

        with tempfile.TemporaryDirectory() as temporary:
            export_set = Path(temporary) / SET_ROOT
            for source in (ROOT / SET_ROOT).rglob("*"):
                relative = source.relative_to(ROOT)
                if source.is_file() and not is_excluded(relative.as_posix()):
                    destination = Path(temporary) / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
            exported = load_set(export_set, court_result.court_map.courts)
            self.assertTrue(exported.ok, exported.findings)
