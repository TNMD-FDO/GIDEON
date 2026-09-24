"""Contract tests for the converted General smoke suite."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast

import yaml  # type: ignore[import-untyped]

from gideon.evaluation.evalset import SET_ROOT, load_set
from gideon.evaluation.turns.cases import CaseSet, load_cases
from tools.exportboundary import is_excluded

ROOT = Path(__file__).resolve().parents[1]
SEED_PATH = Path("eval/seed/general/smoke.yaml")
SUITE_PATH = SET_ROOT / "general" / "smoke.jsonl"
IDS_PATH = SET_ROOT / "slices" / "general-smoke" / "smoke.ids"


def _records(path: Path) -> tuple[dict[str, object], ...]:
    return tuple(cast(dict[str, object], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines())


def _expected(source: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "expect": source["expect"],
        "block": source.get("block", "any"),
        "sources": source.get("sources", "any"),
        "search": source.get("search", False),
        "must": source.get("must", []),
        "must_not": source.get("must_not", []),
    }
    for key in ("must", "must_not"):
        if isinstance(result[key], str):
            result[key] = [result[key]]
    return result


class GeneralSmokeSetContract(unittest.TestCase):
    """The committed set remains a lossless, ordered conversion of its seed."""

    def test_jsonl_prefix_is_canonical_and_holds_to_the_seed(self) -> None:
        suite_path = ROOT / SUITE_PATH
        seed_path = ROOT / SEED_PATH
        records = _records(suite_path)
        seed_document = yaml.safe_load(seed_path.read_text(encoding="utf-8"))
        seed_cases = cast(list[dict[str, object]], seed_document["cases"])

        self.assertEqual(len(suite_path.read_bytes().splitlines()), 14)
        self.assertEqual(len(records), len(seed_cases))
        for source, record in zip(seed_cases, records, strict=True):
            with self.subTest(case=source["id"]):
                self.assertEqual(
                    tuple(record),
                    (
                        "id",
                        "suite",
                        "category",
                        "branch",
                        "question",
                        "expected",
                        "labels",
                        "cluster_id",
                        "review",
                        *(('supersedes',) if "supersedes" in source else ()),
                        "notes",
                    ),
                )
                self.assertEqual(record["id"], source["id"])
                self.assertEqual(record["suite"], "general")
                self.assertEqual(record["category"], "smoke")
                self.assertEqual(record["branch"], "general")
                self.assertEqual(
                    cast(str, record["question"]).encode("utf-8"),
                    cast(str, source["prompt"]).encode("utf-8"),
                )
                self.assertEqual(record["expected"], _expected(source))
                self.assertEqual(record["labels"], ["invented"])
                self.assertEqual(record["cluster_id"], source["id"])
                self.assertEqual(record["review"], {"by": "CSA-1", "on": "2026-09-23"})
                self.assertEqual(record.get("supersedes"), source.get("supersedes"))
                self.assertEqual(record["notes"], "")

        seed_prefix = []
        for source in seed_cases:
            converted: dict[str, object] = {
                "id": source["id"],
                "suite": "general",
                "category": "smoke",
                "branch": "general",
                "question": source["prompt"],
                "expected": _expected(source),
                "labels": ["invented"],
                "cluster_id": source["id"],
                "review": {"by": "CSA-1", "on": "2026-09-23"},
            }
            if "supersedes" in source:
                converted["supersedes"] = source["supersedes"]
            converted["notes"] = ""
            seed_prefix.append(
                json.dumps(converted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
        self.assertTrue(suite_path.read_bytes().startswith(b"".join(seed_prefix)))
        source_cases = load_cases(seed_path)
        self.assertIsInstance(source_cases, CaseSet)
        assert isinstance(source_cases, CaseSet)
        retired_ids = {
            cast(str, source["supersedes"])
            for source in seed_cases
            if "supersedes" in source
        }
        self.assertEqual(
            tuple(case.id for case in source_cases.cases),
            tuple(record["id"] for record in records if record["id"] not in retired_ids),
        )

    def test_id_list_and_suite_counts_match_the_seed(self) -> None:
        records = _records(ROOT / SUITE_PATH)
        seed_document = yaml.safe_load((ROOT / SEED_PATH).read_text(encoding="utf-8"))
        seed_cases = cast(list[dict[str, object]], seed_document["cases"])
        ids = [cast(str, record["id"]) for record in records]
        self.assertEqual((ROOT / IDS_PATH).read_text(encoding="utf-8").splitlines(), ids)
        superseded = {
            cast(str, source["supersedes"])
            for source in seed_cases
            if "supersedes" in source
        }
        active = [case_id for case_id in ids if case_id not in superseded]
        self.assertEqual((len(ids), len(active), len(superseded)), (14, 11, 3))
        self.assertEqual(
            sum(bool(cast(dict[str, object], record["expected"])["search"]) for record in records),
            1,
        )

    def test_suite_loads_in_development_tree_and_export_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            export_root = Path(directory)
            for source in (ROOT / SET_ROOT).rglob("*"):
                if not source.is_file():
                    continue
                relative = source.relative_to(ROOT).as_posix()
                if is_excluded(relative):
                    continue
                destination = export_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

            for set_root in (ROOT / SET_ROOT, export_root / SET_ROOT):
                with self.subTest(set_root=set_root):
                    result = load_set(set_root)
                    self.assertTrue(result.ok, result.findings)
                    assert result.loaded is not None
                    records = result.loaded.cases_by_file["general/smoke.jsonl"]
                    ids = tuple(cast(str, record["id"]) for record in records)
                    self.assertEqual(
                        result.loaded.slice_lists["general-smoke"]["smoke"], tuple(sorted(ids))
                    )
                    active = set(result.loaded.active_ids) & set(ids)
                    self.assertEqual(len(active), 11)
