"""Check, review, and append extraction variants without importing GIDEON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import RESPELLED_TYPES, VariantRefused, derive
from . import axes as axis_registry

_ID = re.compile(r"^extraction-(\d+)$")
def _read(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    return records


def _retired(records: Sequence[Mapping[str, Any]]) -> set[str]:
    superseded = {
        value
        for record in records
        if isinstance(value := record.get("supersedes"), str)
    }
    parents = {
        record["id"]: record["parent"]
        for record in records
        if isinstance(record.get("id"), str) and isinstance(record.get("parent"), str)
    }
    retired: set[str] = set()
    for record in records:
        identifier = record.get("id")
        if not isinstance(identifier, str):
            continue
        current = identifier
        seen: set[str] = set()
        while current not in superseded:
            if current in seen or current not in parents:
                break
            seen.add(current)
            current = parents[current]
        else:
            retired.add(identifier)
    return retired


def _axis_by_id() -> dict[str, axis_registry.Axis]:
    return {axis.axis_id: axis for axis in axis_registry.AXES}


def _current_axes() -> tuple[axis_registry.Axis, ...]:
    """The registry's axes at their current version, in registry order."""

    return tuple(
        axis for axis in axis_registry.AXES if axis_registry.CURRENT[axis.name] is axis
    )


def _origin(record: Mapping[str, Any]) -> str | None:
    labels = record.get("labels")
    if isinstance(labels, list) and labels and isinstance(labels[0], str):
        return labels[0]
    return None


def _axis_id(record: Mapping[str, Any]) -> str | None:
    labels = record.get("labels")
    if isinstance(labels, list):
        for value in labels[1:]:
            if isinstance(value, str) and "@" in value:
                return value
    return None


def _parents(
    parent_records: Sequence[Mapping[str, Any]], all_records: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], ...]:
    retired = _retired(all_records)
    return tuple(
        record
        for record in parent_records
        if isinstance(record.get("id"), str)
        and record["id"] not in retired
        and _origin(record) != "variant"
    )


def _variant_pairs(records: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for record in records:
        parent = record.get("parent")
        axis_id = _axis_id(record)
        if isinstance(parent, str) and axis_id is not None:
            pairs.add((parent, axis_id))
    return pairs


def _highest_id(records: Iterable[Mapping[str, Any]]) -> int:
    highest = 0
    for record in records:
        identifier = record.get("id")
        if isinstance(identifier, str) and (match := _ID.fullmatch(identifier)) is not None:
            highest = max(highest, int(match.group(1)))
    return highest


def _series_files(manifest: Path) -> tuple[list[Path], list[Path]]:
    """The series' files as the manifest beside the cases names them, and the absent ones."""

    named = [
        manifest.parent / line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return named, [path for path in named if not path.is_file()]


def _series_gap(records: Iterable[Mapping[str, Any]]) -> str | None:
    """The first id missing from, or duplicated in, a series that must be whole."""

    numbers = sorted(
        int(match.group(1))
        for record in records
        if isinstance(identifier := record.get("id"), str)
        and (match := _ID.fullmatch(identifier)) is not None
    )
    for position, number in enumerate(numbers, start=1):
        if number != position:
            return f"extraction-{position:03d}"
    return None


def _edits(parent: Mapping[str, Any], axis: axis_registry.Axis) -> list[axis_registry.Edit]:
    question = parent.get("question")
    expected = parent.get("expected")
    objects = expected.get("objects") if isinstance(expected, Mapping) else None
    if not isinstance(question, str) or not isinstance(objects, list):
        return []
    edits: list[axis_registry.Edit] = []
    for label in objects:
        if isinstance(label, Mapping) and label.get("type") in RESPELLED_TYPES:
            edit = axis.function(question, label)
            if edit is not None:
                edits.append(edit)
    return edits


def _missing(
    parents: Sequence[Mapping[str, Any]],
    variants: Sequence[Mapping[str, Any]],
) -> tuple[list[tuple[str, Mapping[str, Any], axis_registry.Axis]], bool]:
    """The pairs the file lacks, and whether every derivation was accepted."""

    existing = _variant_pairs(variants)
    result: list[tuple[str, Mapping[str, Any], axis_registry.Axis]] = []
    okay = True
    for parent in parents:
        parent_id = parent.get("id")
        if not isinstance(parent_id, str):
            continue
        for axis in _current_axes():
            if (parent_id, axis.axis_id) in existing:
                continue
            try:
                derived = derive(parent, axis)
            except VariantRefused as refusal:
                print(f"refused {refusal}")
                okay = False
                continue
            if derived is not None:
                result.append((parent_id, parent, axis))
    return result, okay


def _check(
    parent_records: Sequence[Mapping[str, Any]],
    variant_records: Sequence[Mapping[str, Any]],
) -> tuple[bool, list[tuple[str, Mapping[str, Any], axis_registry.Axis]]]:
    by_id = {
        record["id"]: record
        for record in parent_records
        if isinstance(record.get("id"), str)
    }
    axes = _axis_by_id()
    okay = True
    for variant in variant_records:
        variant_id = variant.get("id", "<missing-id>")
        parent_id = variant.get("parent")
        axis_name = _axis_id(variant)
        axis = axes.get(axis_name) if isinstance(axis_name, str) else None
        parent = by_id.get(parent_id) if isinstance(parent_id, str) else None
        if axis is None or parent is None:
            print(f"refused {variant_id}")
            okay = False
            continue
        try:
            result = derive(parent, axis)
        except VariantRefused:
            print(f"refused {variant_id}")
            okay = False
            continue
        expected = variant.get("expected")
        actual_objects = expected.get("objects") if isinstance(expected, Mapping) else None
        if (
            result is None
            or result[0] != variant.get("question")
            or result[1] != actual_objects
        ):
            print(f"refused {variant_id}")
            okay = False
    missing, derived_okay = _missing(
        _parents(parent_records, (*parent_records, *variant_records)), variant_records
    )
    return okay and derived_okay, missing


def _print_sheet(missing: Sequence[tuple[str, Mapping[str, Any], axis_registry.Axis]]) -> None:
    for identifier, parent, axis in missing:
        print(f"{identifier} parent={parent['id']} axis={axis.axis_id}")
        question = parent["question"]
        for edit in _edits(parent, axis):
            print(
                f"  edit {edit.start}:{edit.end} "
                f"{question[edit.start:edit.end]!r} -> {edit.replacement!r}"
            )


def _variant_record(
    identifier: str,
    parent: Mapping[str, Any],
    axis: axis_registry.Axis,
    question: str,
    objects: list[dict[str, Any]],
    supersedes: str | None,
    reviewer: str,
    review_date: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": identifier,
        "suite": parent.get("suite", "build-gates"),
        "category": parent.get("category", "extraction"),
        "branch": parent.get("branch", "legal"),
        "question": question,
        "expected": {"objects": objects},
        "labels": ["variant", axis.axis_id],
        "parent": parent["id"],
        "cluster_id": parent.get("cluster_id", ""),
        "review": {"by": reviewer, "on": review_date},
    }
    if supersedes is not None:
        record["supersedes"] = supersedes
    record["notes"] = ""
    return record


def _write(
    path: Path,
    missing: Sequence[tuple[str, Mapping[str, Any], axis_registry.Axis]],
    variants: Sequence[Mapping[str, Any]],
    reviewer: str,
    review_date: str,
) -> None:
    previous = {
        (record.get("parent"), _axis_id(record)): record.get("id")
        for record in variants
    }
    lines: list[str] = []
    for identifier, parent, axis in missing:
        result = derive(parent, axis)
        if result is None:
            continue
        old_axis = f"{axis.name}@{axis.version - 1}"
        old_variant = previous.get((parent.get("id"), old_axis))
        supersedes = old_variant if isinstance(old_variant, str) else None
        record = _variant_record(
            identifier,
            parent,
            axis,
            result[0],
            result[1],
            supersedes,
            reviewer,
            review_date,
        )
        lines.append(json.dumps(record, ensure_ascii=False))
    if lines:
        with path.open("a", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("parents", type=Path)
    parser.add_argument("variants", type=Path)
    parser.add_argument("--series", action="append", type=Path, default=[])
    parser.add_argument("--series-file", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sheet", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--reviewer")
    parser.add_argument("--on", dest="review_date")
    args = parser.parse_args(argv)
    if args.write and (args.reviewer is None or args.review_date is None):
        parser.error("--write requires --reviewer and --on")
    parent_records = _read(args.parents)
    variant_records = _read(args.variants)
    series_records = [record for path in args.series for record in _read(path)]
    okay, missing = _check(parent_records, variant_records)
    if not okay:
        return 1
    # The ids come from the whole series, so a write reads every file the
    # manifest beside the cases names — a file it cannot see would have its
    # ids minted twice.  Without a manifest the series is what the command
    # line names, and it must then be whole on its own.
    manifest = args.series_file or args.parents.parent / "series.txt"
    if manifest.is_file():
        named, absent = _series_files(manifest)
        if absent and args.write:
            missing_files = ", ".join(str(path.name) for path in absent)
            print(
                f"refused: {missing_files} named by {manifest} is absent; "
                "a write runs in a full checkout, which holds the whole id series"
            )
            return 1
        series = tuple(
            record for path in named if path.is_file() for record in _read(path)
        )
    else:
        series = (*parent_records, *variant_records, *series_records)
        if args.write and (gap := _series_gap(series)) is not None:
            print(f"refused: the id series is incomplete at {gap}; name every file with --series")
            return 1
    next_id = _highest_id(series)
    missing = [
        (f"extraction-{next_id + offset:03d}", parent, axis)
        for offset, (_, parent, axis) in enumerate(missing, start=1)
    ]
    if args.sheet:
        _print_sheet(missing)
        return 0
    if args.write:
        _write(
            args.variants,
            missing,
            variant_records,
            args.reviewer,
            args.review_date,
        )
    else:
        for identifier, parent, axis in missing:
            print(f"missing {identifier} parent={parent['id']} axis={axis.axis_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
