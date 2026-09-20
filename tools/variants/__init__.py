"""Derive extraction variants by applying standard-library axis edits."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .axes import AXES, CURRENT, RESPELLED_TYPES, Axis, Edit, kept_core


class VariantRefused(Exception):
    """A variant cannot preserve its parent's labelled objects."""


def _refuse(parent_case: Mapping[str, Any], axis: Axis) -> VariantRefused:
    parent_id = parent_case.get("id", "<missing-id>")
    return VariantRefused(f"{parent_id} {axis.axis_id}")


def _labels(parent_case: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected = parent_case.get("expected")
    if not isinstance(expected, Mapping):
        return []
    objects = expected.get("objects")
    if not isinstance(objects, list):
        return []
    return [dict(label) for label in objects if isinstance(label, Mapping)]


def _valid_edit(
    question: str,
    labels: list[dict[str, Any]],
    label_index: int,
    edit: Edit,
    parent_case: Mapping[str, Any],
    axis: Axis,
) -> None:
    if edit.start < 0 or edit.end < edit.start or edit.end > len(question):
        raise _refuse(parent_case, axis)
    if edit.object_start < 0 or edit.object_end < edit.object_start:
        raise _refuse(parent_case, axis)
    if edit.object_end > len(edit.replacement):
        raise _refuse(parent_case, axis)
    label = labels[label_index]
    label_start = label.get("start")
    label_end = label.get("end")
    if (
        not isinstance(label_start, int)
        or not isinstance(label_end, int)
        or edit.start > label_start
        or edit.end < label_end
    ):
        raise _refuse(parent_case, axis)
    for other_index, other in enumerate(labels):
        if other_index == label_index:
            continue
        other_start = other.get("start")
        other_end = other.get("end")
        if (
            isinstance(other_start, int)
            and isinstance(other_end, int)
            and edit.start < other_end
            and other_start < edit.end
        ):
            raise _refuse(parent_case, axis)
    original = question[label_start:label_end]
    replacement_object = edit.replacement[edit.object_start : edit.object_end]
    if kept_core(original, label) != kept_core(replacement_object, label):
        raise _refuse(parent_case, axis)


def derive(parent_case: Mapping[str, Any], axis: Axis) -> tuple[str, list[dict[str, Any]]] | None:
    """Return the respelled question and expected objects, or ``None``."""

    question = parent_case.get("question")
    if not isinstance(question, str):
        raise _refuse(parent_case, axis)
    labels = _labels(parent_case)
    edits: list[tuple[int, Edit]] = []
    for index, label in enumerate(labels):
        if label.get("type") not in RESPELLED_TYPES:
            continue
        edit = axis.function(question, label)
        if edit is not None:
            _valid_edit(question, labels, index, edit, parent_case, axis)
            edits.append((index, edit))
    if not edits:
        return None
    for position, (_, left) in enumerate(edits):
        for _, right in edits[position + 1 :]:
            if left.start < right.end and right.start < left.end:
                raise _refuse(parent_case, axis)

    current = question
    for label_index, edit in sorted(edits, key=lambda value: value[1].start, reverse=True):
        current = current[: edit.start] + edit.replacement + current[edit.end :]
        delta = len(edit.replacement) - (edit.end - edit.start)
        for index, label in enumerate(labels):
            start = label.get("start")
            end = label.get("end")
            if not isinstance(start, int) or not isinstance(end, int):
                raise _refuse(parent_case, axis)
            if index == label_index:
                new_start = edit.start + edit.object_start
                new_end = edit.start + edit.object_end
                label["start"] = new_start
                label["end"] = new_end
                label["text"] = current[new_start:new_end]
            elif start >= edit.end:
                label["start"] = start + delta
                label["end"] = end + delta
                label["text"] = current[start + delta : end + delta]
    return current, labels


__all__ = [
    "AXES",
    "CURRENT",
    "RESPELLED_TYPES",
    "Axis",
    "Edit",
    "VariantRefused",
    "derive",
]
