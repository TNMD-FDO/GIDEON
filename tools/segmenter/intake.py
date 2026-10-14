"""Turn reviewed paragraph marks into immutable segmenter labels."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from gideon.evaluation import evalset, segmenter_draft, segmenter_labels
from gideon.host.render.worker import SECTION_TYPES
from tools.segmenter import manifest

_HEX: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
# The number leads the line; whatever an editor did to the text after it is ignored.
_PARAGRAPH_LINE: Final[re.Pattern[str]] = re.compile(r"([0-9]{4,})(?:\s(.*))?")


class IntakeError(Exception):
    """A content-free, located refusal and its fix."""

    def __init__(self, stage: str, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.stage = stage
        self.problem = problem
        self.fix = fix


@dataclass(frozen=True, slots=True)
class ManifestCase:
    """The text-bound, content-free facts needed to read one sheet."""

    case_id: str
    opinion_id: int
    sha256: str
    length: int
    starts: tuple[int, ...]
    status: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """One draft run's set identity and cases."""

    set_root: Path
    set_digest: str
    slice_pins: Mapping[str, tuple[int, str]]
    cases: tuple[ManifestCase, ...]


def _manifest_problem() -> IntakeError:
    return IntakeError("manifest", "manifest shape is invalid", "Use a manifest from segmenter draft, then retry.")


def _hex(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _load_manifest(path: Path) -> Manifest:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise IntakeError("manifest", "manifest cannot be read", "Provide a readable segmenter draft manifest, then retry.") from error
    if not isinstance(value, dict) or tuple(value) != manifest.MANIFEST_KEYS:
        raise _manifest_problem()
    set_root, digest, prompt, prompt_digest, raw_cases = (
        value["set_root"], value["set_digest"], value["prompt"], value["prompt_digest"], value["cases"],
    )
    if (
        type(value["format"]) is not int or value["format"] != manifest.MANIFEST_FORMAT
        or not isinstance(set_root, str) or not Path(set_root).is_absolute()
        or not _hex(digest) or not isinstance(prompt, str)
        or prompt not in segmenter_draft.PROMPTS
        or prompt_digest != segmenter_draft.PROMPT_DIGESTS[prompt]
        or not isinstance(raw_cases, list) or not raw_cases
    ):
        raise _manifest_problem()
    raw_pins = value["slice_pins"]
    if not isinstance(raw_pins, dict) or set(raw_pins) != {
        manifest.CASE_PATH.as_posix(), manifest.IDS_PATH.as_posix(),
    }:
        raise _manifest_problem()
    slice_pins: dict[str, tuple[int, str]] = {}
    for relative_path, pin in raw_pins.items():
        if (
            not isinstance(pin, list) or len(pin) != 2
            or type(pin[0]) is not int or pin[0] <= 0 or not _hex(pin[1])
        ):
            raise _manifest_problem()
        slice_pins[relative_path] = (pin[0], pin[1])
    cases: list[ManifestCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict) or tuple(raw) != manifest.CASE_KEYS:
            raise _manifest_problem()
        case_id = raw["case_id"]
        starts = raw["starts"]
        if (
            not isinstance(case_id, str) or evalset.SEGMENTER_ID_PATTERN.fullmatch(case_id) is None
            or case_id in seen or type(raw["opinion_id"]) is not int or raw["opinion_id"] <= 0
            or not _hex(raw["doc_id"]) or not _hex(raw["sha256"])
            or type(raw["length"]) is not int or raw["length"] <= 0
            or not isinstance(starts, list) or not starts
            or any(type(start) is not int or start < 0 or start >= raw["length"] for start in starts)
            or any(right <= left for left, right in zip(starts, starts[1:], strict=False))
            or raw["status"] not in ("drafted", "failed")
            or (raw["status"] == "drafted" and raw["failure_code"] is not None)
            or (raw["status"] == "failed" and not isinstance(raw["failure_code"], str))
            or not _hex(raw["sheet_sha256"])
        ):
            raise _manifest_problem()
        seen.add(case_id)
        cases.append(ManifestCase(case_id, raw["opinion_id"], raw["sha256"], raw["length"], tuple(starts), raw["status"]))
    if tuple(case.case_id for case in cases) != tuple(sorted(seen)):
        raise _manifest_problem()
    assert isinstance(set_root, str) and isinstance(digest, str)
    return Manifest(Path(set_root), digest, slice_pins, tuple(cases))


def _set_state(manifest_value: Manifest, set_root: Path,
               cases: Sequence[ManifestCase]) -> evalset.LoadedSet:
    if manifest_value.set_root.name != set_root.resolve().name:
        raise IntakeError("set", "set version differs from the manifest", f"Use a set named {manifest_value.set_root.name}, then retry.")
    loaded_result = evalset.load_set(set_root)
    if loaded_result.loaded is None or loaded_result.findings:
        raise IntakeError("set", "evaluation set cannot be loaded", "Restore the evaluation set, then retry.")
    loaded = loaded_result.loaded
    active = set(loaded.active_ids)
    selected = set(loaded.slices.get("segmenter-300", ()))
    for case in cases:
        record = loaded.cases_by_id.get(case.case_id)
        if (
            case.case_id not in active or case.case_id not in selected
            or case.case_id not in loaded.unsigned_ids or record is None
            or record.get("opinion_id") != case.opinion_id
            or (record.get("suite"), record.get("category")) != ("build-gates", "segmenter-300")
        ):
            raise IntakeError("set", f"case {case.case_id} is no longer active and unsigned in the slice", "Draft this set's unsigned cases again, then retry.")
    fix = "Draft the slice's unsigned cases again, then retry."
    try:
        held = manifest.hold_slice(set_root, manifest_value.slice_pins)
    except OSError as error:
        raise IntakeError("set", "the segmenter slice's files changed since the draft", fix) from error
    if not held:
        raise IntakeError("set", "the segmenter slice's files changed since the draft", fix)
    return loaded


def _sheet_problem(case_id: str, line: int, rule: str) -> IntakeError:
    return IntakeError("sheets", f"case {case_id} line {line}: {rule}", "Correct the paragraph marks, then retry.")


def _marks(path: Path, case: ManifestCase, court: str) -> tuple[tuple[int, str], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise IntakeError("sheets", f"case {case.case_id} sheet cannot be read", "Provide the reviewed sheet, then retry.") from error
    if not lines or lines[0] != manifest.sheet_header(
        case.case_id, case.opinion_id, court, case.sha256, case.length, len(case.starts), case.status,
    ):
        raise _sheet_problem(case.case_id, 1, "sheet header does not match the manifest")
    try:
        header_index = lines.index(manifest.SHEET_PARAGRAPHS_HEADER)
    except ValueError as error:
        raise _sheet_problem(case.case_id, 1, "paragraph heading is missing") from error
    seen: set[int] = set()
    marks: list[tuple[int, str]] = []
    for line_number, line in enumerate(lines[header_index + 1:], header_index + 2):
        if not line.strip():
            continue
        match = _PARAGRAPH_LINE.fullmatch(line.strip())
        if match is None:
            raise _sheet_problem(case.case_id, line_number, "paragraph number is invalid")
        paragraph = int(match.group(1))
        if paragraph >= len(case.starts):
            raise _sheet_problem(case.case_id, line_number, "paragraph is outside the table")
        if paragraph in seen:
            raise _sheet_problem(case.case_id, line_number, "paragraph appears twice")
        if paragraph != len(seen):
            raise _sheet_problem(case.case_id, line_number, "paragraphs are out of order")
        seen.add(paragraph)
        rest = (match.group(2) or "").lstrip()
        if rest.startswith("["):
            close = rest.find("]")
            if close < 0:
                raise _sheet_problem(case.case_id, line_number, "mark is malformed")
            section_type = rest[1:close].strip()
            if section_type not in SECTION_TYPES:
                raise _sheet_problem(case.case_id, line_number, "mark type is unknown")
            marks.append((paragraph, section_type))
    if seen != set(range(len(case.starts))):
        raise _sheet_problem(case.case_id, header_index + len(lines[header_index + 1:]) + 1, "paragraph table is incomplete")
    if not marks or marks[0][0] != 0:
        raise _sheet_problem(case.case_id, header_index + 2, "first paragraph must be marked")
    return tuple(sorted(marks))


def _label(case: ManifestCase, marks: Sequence[tuple[int, str]], signer: str, on: str) -> segmenter_labels.SegmenterLabel:
    spans: list[segmenter_labels.LabelSpan] = []
    for index, (paragraph, section_type) in enumerate(marks):
        start = 0 if index == 0 else case.starts[paragraph]
        end = case.starts[marks[index + 1][0]] if index + 1 < len(marks) else case.length
        spans.append(segmenter_labels.LabelSpan(start, end, section_type))
    label = segmenter_labels.SegmenterLabel(case.case_id, case.sha256, case.length, tuple(spans), signer, on)
    value = json.loads(segmenter_labels.serialize(label))
    findings = segmenter_labels.validate_line(value)
    if findings:
        raise IntakeError("validate", f"case {case.case_id} label spans are invalid", "Correct its paragraph marks, then retry.")
    return label


def intake(manifest_path: Path, sheets: Path, *, signer: str, on: str,
           set_root: Path | None, dry_run: bool, stdout: TextIO,
           requested: Sequence[str] = ()) -> None:
    """Validate selected sheets, then append the labels as one file replacement."""

    if evalset.ROLE_PATTERN.fullmatch(signer) is None or not evalset.valid_iso_date(on):
        raise IntakeError("intake", "signer or date is invalid", "Provide a role id and ISO date, then retry.")
    manifest_value = _load_manifest(manifest_path)
    ids = {case.case_id for case in manifest_value.cases}
    for case_id in requested:
        if evalset.SEGMENTER_ID_PATTERN.fullmatch(case_id) is None or case_id not in ids:
            raise IntakeError("intake", f"case {case_id} is not held by the manifest", "Name a case the manifest holds, then retry.")
    selected_ids = set(requested) if requested else ids
    cases = tuple(case for case in manifest_value.cases if case.case_id in selected_ids)
    target = manifest_value.set_root if set_root is None else set_root
    loaded = _set_state(manifest_value, target, cases)
    labels: list[segmenter_labels.SegmenterLabel] = []
    for case in cases:
        court = loaded.cases_by_id[case.case_id]["court"]
        assert isinstance(court, str)
        labels.append(_label(case, _marks(sheets / f"{case.case_id}.txt", case, court), signer, on))
    path = segmenter_labels.path_for(target)
    try:
        existing = path.read_bytes() if path.exists() else b""
    except OSError as error:
        raise IntakeError("write", "labels file cannot be read", "Restore the labels file, then retry.") from error
    parsed = segmenter_labels.parse_bytes(existing)
    if parsed.findings or any(item.record.case_id in {label.case_id for label in labels} for item in parsed.records):
        raise IntakeError("write", "labels file is invalid or already holds a case", "Restore the labels file or draft an unsigned successor case.")
    added = "".join(segmenter_labels.serialize(label) for label in labels).encode("utf-8")
    payload = existing + added
    if not dry_run:
        try:
            manifest.write_atomic(path, payload)
        except OSError as error:
            raise IntakeError("write", "labels cannot be appended", "Use a writable set directory, then retry.") from error
    for label in labels:
        counts = Counter(span.section_type for span in label.sections)
        figures = ", ".join(f"{name}={counts[name]}" for name in sorted(counts))
        print(f"{label.case_id}: {len(label.sections)} spans; {figures}", file=stdout)
    digest_state = "unchanged" if loaded.digest == manifest_value.set_digest else "moved"
    print(f"set digest: {digest_state} since the draft", file=stdout)
    not_taken = tuple(case.case_id for case in manifest_value.cases if case.case_id not in selected_ids)
    print(f"not taken: {len(not_taken)} ids {', '.join(not_taken)}" if not_taken else "not taken: none", file=stdout)
    print(f"labels: {len(parsed.records) + len(labels)} lines sha256 {hashlib.sha256(payload).hexdigest()}" + (" (dry-run)" if dry_run else ""), file=stdout)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Intake corrected segmenter sheets")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("sheets", type=Path)
    parser.add_argument("--signer", required=True)
    parser.add_argument("--on", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--set", dest="set_root", type=Path)
    parser.add_argument("--case", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    """Read corrected sheets and return the intake command status."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        intake(args.manifest, args.sheets, signer=args.signer, on=args.on,
               set_root=args.set_root, dry_run=args.dry_run, stdout=output,
               requested=args.case)
    except IntakeError as error:
        print(f"{error.stage}: refused — {error.problem}. Fix: {error.fix}", file=errors)
        return 1
    except Exception as error:  # noqa: BLE001 - command boundary keeps sheet text out of diagnostics
        print(f"segmenter intake: internal error ({type(error).__name__})", file=errors)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
