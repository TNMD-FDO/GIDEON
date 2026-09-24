"""Contract tests for the converted guardrails cases and frozen lists."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Final, cast

import yaml  # type: ignore[import-untyped]

from gideon.evaluation.evalset import SET_ROOT, load_set
from gideon.evaluation.guardrails_slice import FRONTEND_SAMPLE
from tools.exportboundary import absent_from_export, is_excluded

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
GUARDRAILS_PATH: Final[Path] = SET_ROOT / "guardrails"
SEED_PATH: Final[Path] = Path("eval/seed/guardrails")
PINNED_PREFIXES: Final[tuple[tuple[str, int, str], ...]] = (
    ("deadline-trap", 105, "d38ceb4e875111cf7503b8c2e378204e38168996e36f36e8a6859ff3d5828f76"),
    ("guidelines-range", 96, "c25da2f636b208315f61935299d2fa3cea4e5a04084fb239912f232ae3502e0b"),
    ("sentence-credit", 89, "d7efe667a7b88e9811452162ed1ebda5c4937bf68bee4e45454ffe4c77a2c03f"),
)
# The counts at the conversion: lines, active, active
# positives, active controls, superseded. An append moves them with the pins.
COUNTS: Final[dict[str, tuple[int, int, int, int, int]]] = {
    "deadline-trap": (105, 102, 39, 63, 3),
    "guidelines-range": (96, 87, 45, 42, 9),
    "sentence-credit": (89, 86, 39, 47, 3),
}

_UNDER_TWENTY: Final[tuple[str, ...]] = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS: Final[tuple[str, ...]] = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)
_SMALL_NUMBER: Final[re.Pattern[str]] = re.compile(r"(?<![A-Za-z0-9])([0-9]{1,3})(?![A-Za-z0-9])")


def _number_words(value: int) -> str:
    """Spell one non-negative integer below one thousand with hyphens."""

    if value < 20:
        return _UNDER_TWENTY[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        return _TENS[tens] if ones == 0 else f"{_TENS[tens]}-{_UNDER_TWENTY[ones]}"
    hundreds, remainder = divmod(value, 100)
    prefix = f"{_UNDER_TWENTY[hundreds]} hundred"
    return prefix if remainder == 0 else f"{prefix} {_number_words(remainder)}"


def _spell_small_numbers(text: str) -> str:
    """Spell prose numerals below one thousand while preserving four-digit years."""

    return _SMALL_NUMBER.sub(lambda match: _number_words(int(match.group(1))), text)


def _records(path: Path) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for line in path.read_bytes().splitlines():
        value = json.loads(line)
        if isinstance(value, dict):
            records.append(cast(dict[str, object], value))
    return tuple(records)


class GuardrailsSetContract(unittest.TestCase):
    """The committed suite stays bound to its guardrail seed files."""

    def test_files_are_pinned_by_append_only_prefixes(self) -> None:
        for category, count, digest in PINNED_PREFIXES:
            with self.subTest(category=category):
                path = ROOT / GUARDRAILS_PATH / f"{category}.jsonl"
                if absent_from_export(GUARDRAILS_PATH / f"{category}.jsonl", ROOT):
                    self.skipTest("guardrails suite is absent from this exported tree")
                lines = path.read_bytes().splitlines(keepends=True)
                self.assertEqual(len(lines), count)
                self.assertEqual(hashlib.sha256(b"".join(lines[:count])).hexdigest(), digest)

    def test_cases_and_id_lists_match_seed_order_and_roles(self) -> None:
        if absent_from_export(GUARDRAILS_PATH / "deadline-trap.jsonl", ROOT):
            self.skipTest("guardrails suite is absent from this exported tree")

        for category, _count, _digest in PINNED_PREFIXES:
            with self.subTest(category=category):
                seed = yaml.safe_load(
                    (ROOT / SEED_PATH / f"{category}.yaml").read_text(encoding="utf-8")
                )
                cases = _records(ROOT / GUARDRAILS_PATH / f"{category}.jsonl")
                ids = tuple(f"{category}/{case['id']}" for case in seed["cases"])
                converted_ids = tuple(case.get("id") for case in cases)
                self.assertEqual(converted_ids, ids)
                id_list = ROOT / SET_ROOT / "slices" / "guardrails" / f"{category}.ids"
                self.assertEqual(id_list.read_text(encoding="utf-8").splitlines(), list(ids))
                self.assertEqual(len(cases), len(seed["cases"]))

                retired: set[str] = set()
                role_counts = {"positive": 0, "control": 0}
                for source, converted in zip(seed["cases"], cases, strict=True):
                    source_id = f"{category}/{source['id']}"
                    self.assertEqual(
                        cast(str, converted["question"]).encode("utf-8"),
                        cast(str, source["prompt"]).encode("utf-8"),
                    )
                    self.assertEqual(converted["category"], category)
                    self.assertEqual(converted["branch"], "general")
                    self.assertEqual(converted["cluster_id"], source_id)
                    self.assertEqual(converted["labels"], ["invented", source["kind"]])
                    role_counts[source["kind"]] += 1
                    expected = cast(dict[str, object], converted["expected"])
                    self.assertEqual(
                        expected["turn"], "blocked" if source["kind"] == "positive" else "clean"
                    )
                    if source["kind"] == "positive":
                        self.assertEqual(expected["pattern"], source["pattern"])
                    if source["kind"] == "control":
                        self.assertNotIn("must_not", source)
                    if "must_not" not in source:
                        self.assertNotIn("must_not", expected)
                    else:
                        # The named figure is in the canned answer, digits and
                        # spelled alike, and never in the prompt's inputs.
                        self.assertEqual(expected["must_not"], source["must_not"])
                        values = source["must_not"]
                        patterns = [values] if isinstance(values, str) else cast(list[str], values)
                        compiled = [re.compile(pattern) for pattern in patterns]
                        answer = cast(str, source["answer"])
                        for text in (answer, _spell_small_numbers(answer)):
                            self.assertTrue(
                                any(pattern.search(text) for pattern in compiled), source["id"]
                            )
                        self.assertFalse(
                            any(pattern.search(cast(str, source["prompt"])) for pattern in compiled),
                            source["id"],
                        )
                    if "supersedes" in source:
                        target = f"{category}/{source['supersedes']}"
                        self.assertEqual(converted["supersedes"], target)
                        retired.add(target)
                    else:
                        self.assertNotIn("supersedes", converted)
                active_ids = set(ids) - retired
                self.assertEqual(len(active_ids), len(cases) - len(retired))
                self.assertEqual(sum(role_counts.values()), len(seed["cases"]))
                active_seed_roles = {
                    role: sum(
                        source["kind"] == role
                        and f"{category}/{source['id']}" not in retired
                        for source in seed["cases"]
                    )
                    for role in ("positive", "control")
                }
                active_converted_roles = {
                    role: sum(
                        cast(list[str], converted["labels"])[1] == role
                        and converted["id"] not in retired
                        for converted in cases
                    )
                    for role in ("positive", "control")
                }
                self.assertEqual(active_converted_roles, active_seed_roles)
                self.assertEqual(
                    (
                        len(cases),
                        len(active_ids),
                        active_converted_roles["positive"],
                        active_converted_roles["control"],
                        len(retired),
                    ),
                    COUNTS[category],
                )

    def test_committed_suite_loads_in_the_tree_and_an_export_copy(self) -> None:
        if absent_from_export(GUARDRAILS_PATH / "deadline-trap.jsonl", ROOT):
            self.skipTest("guardrails suite is absent from this exported tree")

        with tempfile.TemporaryDirectory() as directory:
            roots = [ROOT / SET_ROOT]
            export_root = Path(directory)
            export_set = export_root / SET_ROOT
            for source in (ROOT / SET_ROOT).rglob("*"):
                if not source.is_file():
                    continue
                relative = source.relative_to(ROOT).as_posix()
                if is_excluded(relative):
                    continue
                destination = export_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            roots.append(export_set)

            for set_root in roots:
                with self.subTest(set_root=set_root):
                    result = load_set(set_root)
                    self.assertTrue(result.ok, result.findings)
                    assert result.loaded is not None
                    for category, _count, _digest in PINNED_PREFIXES:
                        cases = result.loaded.cases_by_file[f"guardrails/{category}.jsonl"]
                        ids = tuple(cast(str, case["id"]) for case in cases)
                        self.assertEqual(
                            result.loaded.slice_lists["guardrails"][category], tuple(sorted(ids))
                        )
                        active = [
                            case_id
                            for case_id in result.loaded.active_ids
                            if case_id.startswith(f"{category}/")
                        ]
                        self.assertEqual(len(active), COUNTS[category][1])

    def test_frontend_sample_ids_are_active_and_cover_both_roles_per_family(self) -> None:
        if absent_from_export(GUARDRAILS_PATH / "deadline-trap.jsonl", ROOT):
            self.skipTest("guardrails suite is absent from this exported tree")
        loaded_result = load_set(ROOT / SET_ROOT)
        self.assertTrue(loaded_result.ok, loaded_result.findings)
        assert loaded_result.loaded is not None
        loaded = loaded_result.loaded
        self.assertEqual(len(FRONTEND_SAMPLE), 6)
        self.assertTrue(set(FRONTEND_SAMPLE) <= set(loaded.active_ids))
        roles_by_family: dict[str, set[str]] = {}
        for case_id in FRONTEND_SAMPLE:
            record = loaded.cases_by_id[case_id]
            roles_by_family.setdefault(cast(str, record["category"]), set()).add(
                cast(list[str], record["labels"])[1]
            )
        self.assertEqual(
            roles_by_family,
            {
                family: {"positive", "control"}
                for family in ("deadline-trap", "guidelines-range", "sentence-credit")
            },
        )
