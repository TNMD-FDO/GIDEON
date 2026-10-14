"""Run the opinion walk and segmenter in the pinned image, replying with the anchorer's cross-check counts."""

import hashlib
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from gideon.evaluation.results import RunContext
from gideon.host.report import Problem

# The module is the image's entry as well as the host's leg, and the image lacks
# the host's YAML loader, so imports reaching it stay inside the host functions.

CONTAINER_PREFIX: Final[str] = "gideon-segmenter-"
# exempt: a starting value for three hundred originals and the container's start, corrected by measurement.
IMAGE_TIMEOUT_SECONDS: Final[int] = 300
REMOVE_TIMEOUT_SECONDS: Final[int] = 30
PROBLEMS: Final[frozenset[str]] = frozenset({"image-leg-failed"})
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
# The evaluation set's segmenter id rule, restated for the image and held equal by a test.
CASE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"segmenter-[0-9]{3,}")
_MARKER_KINDS: Final[frozenset[str]] = frozenset({"page", "footnote-mark", "editorial"})


@dataclass(frozen=True, slots=True)
class LegInput:
    """The record facts needed to re-read one original."""

    case_id: str
    column: str
    opinion_type: str
    per_curiam: bool


@dataclass(frozen=True, slots=True)
class LegSection:
    """One candidate section in canonical-text code points."""

    start: int
    end: int
    section_type: str
    typed_by: str


@dataclass(frozen=True, slots=True)
class LegMarker:
    """A dropped locator's kind, label, and canonical-text offset."""

    kind: str
    label: str
    offset: int


@dataclass(frozen=True, slots=True)
class LegResult:
    """The candidate tree's content-free reading of one original."""

    id: str
    sha256: str
    length: int
    sections: tuple[LegSection, ...]
    markers: tuple[LegMarker, ...]
    pgmap_checked: int
    pgmap_disagreeing: int


def _image_fix(reference: str | None) -> str:
    if reference is None:
        return "Build the pinned gideon image with tools.imagebuild, then retry."
    registry = reference.rsplit("/gideon@", 1)[0]
    return f"Run sudo python3 -m tools.imagebuild gideon --to {registry} --check, then retry."


def _failure(reason: str, reference: str | None) -> Problem:
    return Problem(f"segmenter image leg failed: {reason}", _image_fix(reference))


def _integer(value: object) -> bool:
    return type(value) is int


def _reply(value: object, expected_id: str) -> LegResult | None:
    from gideon.host.render.worker import SECTION_TYPED_BY, SECTION_TYPES

    if not isinstance(value, dict) or tuple(value) != (
        "id", "sha256", "length", "sections", "markers", "pgmap_checked", "pgmap_disagreeing"
    ):
        return None
    case_id, sha256, length = value["id"], value["sha256"], value["length"]
    if (
        case_id != expected_id or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None or not _integer(length) or length <= 0
    ):
        return None
    pgmap_checked, pgmap_disagreeing = value["pgmap_checked"], value["pgmap_disagreeing"]
    if (
        not _integer(pgmap_checked) or not _integer(pgmap_disagreeing)
        or not 0 <= pgmap_disagreeing <= pgmap_checked
    ):
        return None
    raw_sections, raw_markers = value["sections"], value["markers"]
    if not isinstance(raw_sections, list) or not raw_sections or not isinstance(raw_markers, list):
        return None
    sections: list[LegSection] = []
    end = 0
    for part in raw_sections:
        if (
            not isinstance(part, list) or len(part) != 4
            or not _integer(part[0]) or not _integer(part[1])
            or part[0] != end or part[1] <= part[0]
            or part[2] not in SECTION_TYPES or part[3] not in SECTION_TYPED_BY
        ):
            return None
        sections.append(LegSection(part[0], part[1], part[2], part[3]))
        end = part[1]
    if end != length:
        return None
    markers: list[LegMarker] = []
    for item in raw_markers:
        if (
            not isinstance(item, list) or len(item) != 3
            or not isinstance(item[0], str) or item[0] not in _MARKER_KINDS
            or not isinstance(item[1], str)
            or not _integer(item[2]) or not 0 <= item[2] <= length
        ):
            return None
        markers.append(LegMarker(item[0], item[1], item[2]))
    return LegResult(
        case_id, sha256, length, tuple(sections), tuple(markers), pgmap_checked, pgmap_disagreeing
    )


def run_leg(
    cases: Sequence[LegInput], originals: Mapping[str, str], context: RunContext
) -> Mapping[str, LegResult] | Problem:
    """Return every candidate reading from one isolated image run or one refusal."""

    from gideon.host import stack

    image = context.image
    if image is None:
        return _failure("image access unavailable", None)
    reference = image.reference
    try:
        request = "".join(
            json.dumps({
                "id": case.case_id,
                "column": case.column,
                "opinion_type": case.opinion_type,
                "per_curiam": case.per_curiam,
                "original": originals[case.case_id],
            }, ensure_ascii=False) + "\n"
            for case in cases
        )
    except (KeyError, TypeError, ValueError):
        return _failure("input incomplete", reference)
    name = f"{CONTAINER_PREFIX}{uuid.uuid4().hex[:12]}"
    try:
        result = context.host.run(
            stack.image_run_argv(
                reference, image.checkout, "python", "-m", "gideon.evaluation.segmenter_leg",
                name=name,
            ),
            input=request,
            timeout=IMAGE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        try:
            removed = context.host.run(
                stack.container_remove_argv(name), timeout=REMOVE_TIMEOUT_SECONDS
            )
        except (OSError, subprocess.SubprocessError):
            return _failure("container timed out; removal could not run", reference)
        if removed.returncode != 0:
            return _failure(f"container timed out; removal exited {removed.returncode}", reference)
        return _failure("container timed out", reference)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return _failure("container could not run", reference)
    if result.returncode != 0:
        return _failure(f"container exited {result.returncode}", reference)
    lines = result.stdout.splitlines()
    if len(lines) != len(cases):
        return _failure(f"{len(lines)} replies for {len(cases)} cases", reference)
    replies: dict[str, LegResult] = {}
    expected = {case.case_id for case in cases}
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return _failure(f"unreadable reply line {line_number}", reference)
        case_id = value.get("id") if isinstance(value, dict) else None
        if not isinstance(case_id, str) or case_id not in expected or case_id in replies:
            return _failure(f"unexpected reply id at line {line_number}", reference)
        parsed = _reply(value, case_id)
        if parsed is None:
            return _failure(f"invalid reply line {line_number}", reference)
        replies[case_id] = parsed
    if set(replies) != expected:
        return _failure("reply missing an id", reference)
    return replies


def main() -> int:
    """Read originals on stdin and write content-free candidate readings."""

    from gideon.worker import anchors, opiniontext, sections

    for line_number, line in enumerate(sys.stdin, start=1):
        case_id: str | None = None
        try:
            value = json.loads(line)
            if not isinstance(value, dict) or tuple(value) != (
                "id", "column", "opinion_type", "per_curiam", "original"
            ):
                raise ValueError("invalid input shape")
            raw_id = value["id"]
            if not isinstance(raw_id, str) or CASE_ID_PATTERN.fullmatch(raw_id) is None:
                raise ValueError("invalid id")
            case_id = raw_id
            column, opinion_type = value["column"], value["opinion_type"]
            if (
                column not in opiniontext.TEXT_SOURCES
                or not isinstance(opinion_type, str)
                or type(value["per_curiam"]) is not bool
                or not isinstance(value["original"], str)
            ):
                raise ValueError("invalid input fields")
            parsed = opiniontext.canonical_text(column, value["original"])
            segmented = sections.segment(
                parsed, opinion_type=opinion_type,
                per_curiam=value["per_curiam"], column=column,
            )
            anchored = anchors.anchor(parsed, segmented)
            answer = {
                "id": case_id,
                "sha256": hashlib.sha256(parsed.text.encode("utf-8")).hexdigest(),
                "length": len(parsed.text),
                "sections": [
                    [item.char_start, item.char_end, item.section_type, item.typed_by]
                    for item in segmented
                ],
                "markers": [[item.kind, item.label, item.offset] for item in parsed.markers],
                "pgmap_checked": anchored.pgmap_checked,
                "pgmap_disagreeing": anchored.pgmap_disagreeing,
            }
            print(json.dumps(answer, ensure_ascii=False))
        except Exception as error:  # noqa: BLE001 - the image entry must emit no opinion text
            location = case_id or f"line {line_number}"
            print(f"segmenter leg: {location}: {type(error).__name__}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
