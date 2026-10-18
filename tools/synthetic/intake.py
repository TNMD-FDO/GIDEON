"""Append CSA-reviewed synthetic cases after holding their draft and sheet digests."""

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TextIO, cast

from gideon.evaluation import evalset, judgments, synthetic
from gideon.host import courts
from gideon.host.index.sample import SAMPLE_SUFFIX, SOURCE_ID_MAX_LENGTH
from gideon.host.render.worker import GENERATION_PATTERN
from tools.segmenter.intake import IntakeError
from tools.segmenter.manifest import write_atomic
from tools.synthetic import manifest

_HEX: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_MARK_LINE: Final[re.Pattern[str]] = re.compile(r"([0-9]+)\s+(accept|reject)")
_MANIFEST_FIX: Final[str] = "Use a manifest from synthetic draft, then retry."
_DRAFT_FIX: Final[str] = "Restore the drafts and sheet from this synthetic draft, then review them again."
_SET_FIX: Final[str] = "Restore the evaluation set, then retry."
_APPEND_FIX: Final[str] = "Use a writable set directory, then retry."


@dataclass(frozen=True, slots=True)
class Manifest:
    """The content-free provenance and ordered entries of one synthetic draft."""

    set_root: Path
    set_digest: str
    category: str
    case_pin: tuple[int, str] | None
    template: str
    sheet_sha256: str
    sheet_bytes: int
    read_share: int
    read_ordinals: tuple[int, ...]
    entries: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class DraftCase:
    """A digest-held draft whose generated text stays out of diagnostics."""

    ordinal: int
    value: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class Marks:
    """The CSA's role, review date, marked ordinals, and accepted ordinals."""

    by: str
    on: str
    marked: frozenset[int]
    accepted: frozenset[int]


def _hex(value: object) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _manifest_problem(rule: str = "shape") -> IntakeError:
    return IntakeError("manifest", f"manifest {rule} is invalid", _MANIFEST_FIX)


def _load_manifest(path: Path) -> Manifest:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise IntakeError("manifest", "manifest cannot be read", _MANIFEST_FIX) from error
    if not isinstance(value, dict):
        raise _manifest_problem()
    if type(value.get("format")) is not int or value["format"] != manifest.MANIFEST_FORMAT:
        raise IntakeError("manifest", "manifest format is unsupported",
                          "Draft this category again with python3 -m tools.synthetic.draft and review the new sheet, then retry.")
    if tuple(value) != manifest.MANIFEST_KEYS:
        raise _manifest_problem()
    category = value["category"]
    template, reading = value["template"], value["reading"]
    spec = evalset.SHAPE_REGISTRY.get(("research-qa", category)) if isinstance(category, str) else None
    if (
        not isinstance(value["set_root"], str) or not Path(value["set_root"]).is_absolute()
        or not _hex(value["set_digest"]) or not _hex(value["sheet_sha256"])
        or spec is None or not spec.synthetic_keys
        or not isinstance(template, str) or template not in synthetic.TEMPLATES
        or not template.startswith(f"{category}@")
        or value["template_digest"] != synthetic.TEMPLATE_DIGESTS[template]
        or not isinstance(reading, str) or reading not in synthetic.READINGS
        or value["reading_digest"] != synthetic.READING_DIGESTS[reading]
        or not isinstance(value["generation"], str) or re.fullmatch(GENERATION_PATTERN, value["generation"]) is None
        or not value["generation"].endswith(f"__{SAMPLE_SUFFIX}")
        or type(value["seed"]) is not int
        or type(value["asked"]) is not int or value["asked"] <= 0
        or not isinstance(value["entries"], list) or len(value["entries"]) > value["asked"]
        or type(value["read_share"]) is not int
        or value["read_share"] != (1 if spec.zero_tolerance else synthetic.READ_SHARE)
        or not isinstance(value["read_ordinals"], list)
        or any(type(ordinal) is not int for ordinal in value["read_ordinals"])
        or value["read_ordinals"] != list(synthetic.sample_ordinals(len(value["entries"]), value["read_share"]))
        or type(value["sheet_bytes"]) is not int or value["sheet_bytes"] < 0
        or (value["sheet_bytes"] == 0) != (len(value["entries"]) == 0)
        or not isinstance(value["discards"], list)
    ):
        raise _manifest_problem()
    raw_pin = value["case_pin"]
    if raw_pin is not None and (
        not isinstance(raw_pin, list) or len(raw_pin) != 2
        or type(raw_pin[0]) is not int or raw_pin[0] < 0 or not _hex(raw_pin[1])
    ):
        raise _manifest_problem("case pin")
    entries: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for ordinal, entry in enumerate(value["entries"], start=1):
        if not isinstance(entry, dict) or tuple(entry) != manifest.ENTRY_KEYS:
            raise _manifest_problem(f"entry {ordinal:02d} keys or order")
        score, label = entry["closed_book_score"], entry["label"]
        if (
            type(entry["ordinal"]) is not int or entry["ordinal"] != ordinal
            or judgments.coordinate_findings(entry)
            or entry["source_id"] in seen
            or not _hex(entry["doc_id"]) or not _hex(entry["quote_sha256"]) or not _hex(entry["draft_sha256"])
            or type(entry["length"]) is not int or entry["length"] != entry["end"] - entry["start"]
            or type(score) is not int or score not in (0, 1, 2, 3)
            or label != (synthetic.CLOSED_BOOK_LABEL if score >= synthetic.CLOSED_BOOK_SCORE else None)
        ):
            raise _manifest_problem(f"entry {ordinal:02d}")
        seen.add(entry["source_id"])
        entries.append(entry)
    seen_discards: set[tuple[str, str | None, str | None]] = set()
    for entry in value["discards"]:
        if (
            not isinstance(entry, dict) or tuple(entry) != manifest.DISCARD_KEYS
            or entry["reason"] not in manifest.DISCARD_REASONS
            or not isinstance(entry["source_id"], str)
            or not 1 <= len(entry["source_id"]) <= SOURCE_ID_MAX_LENGTH
            or judgments.SOURCE_ID_PATTERN.fullmatch(entry["source_id"]) is None
            or (entry["edge"] is not None and not _hex(entry["edge"]))
            or (entry["kind"] is not None and (
                not isinstance(entry["kind"], str)
                or entry["kind"] not in (*synthetic.TREATMENT_KINDS, *synthetic.ATTRIBUTION_KINDS)
            ))
        ):
            raise _manifest_problem("discards")
        identity = (entry["source_id"], entry["edge"], entry["kind"])
        if identity in seen_discards:
            raise _manifest_problem("discards")
        seen_discards.add(identity)
    pin = None if raw_pin is None else (raw_pin[0], raw_pin[1])
    return Manifest(
        set_root=Path(value["set_root"]), set_digest=value["set_digest"], category=category,
        case_pin=pin, template=template, sheet_sha256=value["sheet_sha256"],
        sheet_bytes=value["sheet_bytes"], read_share=value["read_share"],
        read_ordinals=tuple(value["read_ordinals"]), entries=tuple(entries),
    )


def _drafts(path: Path, provenance: Manifest) -> tuple[DraftCase, ...]:
    try:
        lines = (path.parent / "drafts.jsonl").read_bytes().splitlines(keepends=True)
        sheet = (path.parent / "sheet.txt").read_bytes()
    except OSError as error:
        raise IntakeError("drafts", "drafts or sheet cannot be read", _DRAFT_FIX) from error
    if len(sheet) < provenance.sheet_bytes or hashlib.sha256(sheet[:provenance.sheet_bytes]).hexdigest() != provenance.sheet_sha256:
        raise IntakeError("drafts", "sheet digest differs from the manifest", _DRAFT_FIX)
    if len(lines) < len(provenance.entries):
        raise IntakeError("drafts", "draft has fewer lines than the manifest", _DRAFT_FIX)
    cases: list[DraftCase] = []
    for entry, line in zip(provenance.entries, lines[:len(provenance.entries)], strict=True):
        ordinal = cast(int, entry["ordinal"])
        if not line.endswith(b"\n") or hashlib.sha256(line).hexdigest() != entry["draft_sha256"]:
            raise IntakeError("drafts", f"ordinal {ordinal:02d}: draft digest differs from the manifest", _DRAFT_FIX)
        try:
            value = json.loads(line)
        except (UnicodeError, ValueError) as error:
            raise IntakeError("drafts", f"ordinal {ordinal:02d}: draft is not JSON", _DRAFT_FIX) from error
        if not isinstance(value, dict) or tuple(value) != manifest.DRAFT_KEYS:
            raise IntakeError("drafts", f"ordinal {ordinal:02d}: draft keys or order", _DRAFT_FIX)
        gold, labels, answer = value["gold"], value["labels"], value["closed_book_answer"]
        if (
            type(value["ordinal"]) is not int or value["ordinal"] != ordinal
            or value["category"] != provenance.category or value["template"] != provenance.template
            or not isinstance(gold, list) or not gold or not isinstance(gold[0], dict)
            or any(gold[0].get(key) != entry[key] for key in ("source_id", "start", "end", "sha256"))
            or not isinstance(gold[0].get("quote"), str)
            or len(gold[0]["quote"]) != entry["length"]
            or hashlib.sha256(gold[0]["quote"].encode("utf-8")).hexdigest() != entry["quote_sha256"]
            or labels != [synthetic.SYNTHETIC_ORIGIN] + ([entry["label"]] if entry["label"] is not None else [])
            or not isinstance(answer, str) or not answer.strip() or "\n" in answer or "\r" in answer
        ):
            raise IntakeError("drafts", f"ordinal {ordinal:02d}: draft facts differ from the manifest", _DRAFT_FIX)
        cases.append(DraftCase(ordinal, value))
    return tuple(cases)


def _marks(path: Path, provenance: Manifest) -> Marks:
    fix = "Put synthetic: <CSA role id> <sheet digest prefix> <ISO date> on line 1, then retry."
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise IntakeError("marks", "marks cannot be read", "Provide a readable UTF-8 marks file, then retry.") from error
    header = manifest.MARKS_HEADER_PATTERN.fullmatch(lines[0].strip()) if lines else None
    if header is None:
        raise IntakeError("marks", "line 1: header is invalid", fix)
    role, prefix, on = header.group("role", "digest", "date")
    if evalset.ROLE_PATTERN.fullmatch(role) is None or not role.startswith("CSA-"):
        raise IntakeError("marks", "line 1: reviewer must be a CSA role id", "Have the CSA complete the required read and enter their CSA role id, then retry.")
    if len(prefix) < manifest.DIGEST_PREFIX_CHARS or not provenance.sheet_sha256.startswith(prefix):
        raise IntakeError("marks", "line 1: sheet digest prefix differs from the manifest",
                          "Run python3 -m tools.synthetic.intake <manifest> --template to regenerate the marks template, then complete it and retry.")
    if not evalset.valid_iso_date(on):
        raise IntakeError("marks", "line 1: review date is invalid", "Enter an ISO date in the marks header, then retry.")
    seen: set[int] = set()
    accepted: set[int] = set()
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        match = _MARK_LINE.fullmatch(line.strip())
        problem = None
        if match is None:
            problem = "mark must be an ordinal and accept or reject"
        else:
            ordinal = int(match[1])
            if not 1 <= ordinal <= len(provenance.entries):
                problem = "ordinal is outside the manifest"
            elif ordinal in seen:
                problem = "ordinal is marked twice"
            else:
                seen.add(ordinal)
                if match[2] == "accept":
                    accepted.add(ordinal)
        if problem is not None:
            raise IntakeError("marks", f"line {number}: {problem}", "Use each marked ordinal once with accept or reject, then retry.")
    missing = set(provenance.read_ordinals) - seen
    if missing:
        raise IntakeError("marks", f"line {len(lines) + 1}: ordinal {min(missing):02d} is unmarked", "Mark every read ordinal once with accept or reject, then retry.")
    if len(seen) < len(provenance.entries):
        rejected = len(set(provenance.read_ordinals) - accepted)
        if rejected * synthetic.SAMPLE_REJECTION_CEILING > len(provenance.read_ordinals):
            raise IntakeError("marks", f"{rejected} of {len(provenance.read_ordinals)} read cases rejected exceeds the ceiling",
                              "Read every case and mark every ordinal accept or reject, then retry.")
    return Marks(role, on, frozenset(seen), frozenset(accepted))


def _court_ids() -> frozenset[str]:
    result = courts.load_court_map(courts.default_courts_path())
    if result.errors or result.court_map is None:
        raise IntakeError("set", "court map cannot be loaded", "Restore courts.yaml from the release checkout, then retry.")
    return frozenset(result.court_map.courts)


def intake(manifest_path: Path, marks_path: Path, *, set_root: Path | None,
           dry_run: bool, stdout: TextIO, court_ids: Iterable[str] | None = None) -> None:
    """Validate the whole review, then append accepted cases and verify the set."""

    provenance = _load_manifest(manifest_path)
    cases = _drafts(manifest_path, provenance)
    marks = _marks(marks_path, provenance)
    target = provenance.set_root if set_root is None else set_root
    if target.resolve().name != provenance.set_root.name:
        raise IntakeError("set", "set version differs from the manifest", "Use a set with the manifest's version, then retry.")
    known_courts = _court_ids() if court_ids is None else frozenset(court_ids)
    result = evalset.load_set(target, known_courts)
    if result.findings or result.loaded is None:
        raise IntakeError("set", "evaluation set cannot be loaded", _SET_FIX)
    loaded = result.loaded
    relative = synthetic.case_path(provenance.category)
    path = target / relative
    try:
        held = manifest.hold_file(path, provenance.case_pin)
        try:
            previous = path.read_bytes()
        except FileNotFoundError:
            previous = None
    except OSError as error:
        raise IntakeError("set", "category file cannot be held", _SET_FIX) from error
    if not held:
        raise IntakeError("set", "category file changed since the draft", "Draft this category again and review the new sheet, then retry.")
    existing_cases = loaded.cases_by_file.get(relative, ())
    pattern = synthetic.synthetic_id_pattern(provenance.category)
    for case in existing_cases:
        if (
            not isinstance(case["id"], str) or pattern.fullmatch(case["id"]) is None
            or case["category"] != provenance.category
            or cast(list[str], case["labels"])[:1] != [synthetic.SYNTHETIC_ORIGIN]
        ):
            raise IntakeError("set", "category file holds a different case series", _SET_FIX)
    next_number = int(cast(str, existing_cases[-1]["id"]).rsplit("-", 1)[1]) + 1 if existing_cases else 1
    cased = {
        entry["source_id"] for case_id in loaded.active_ids
        for case in (loaded.cases_by_id[case_id],)
        if case["category"] == provenance.category
        and cast(list[str], case["labels"])[:1] == [synthetic.SYNTHETIC_ORIGIN]
        for entry in cast(list[dict[str, object]], case["gold"])
    }
    added: list[bytes] = []
    rows: list[str] = []
    labelled = 0
    review_keys = evalset.SHAPE_REGISTRY[("research-qa", provenance.category)].review_keys_for(synthetic.SYNTHETIC_ORIGIN)
    for draft_case in cases:
        if draft_case.ordinal in marks.marked and draft_case.ordinal not in marks.accepted:
            rows.append(f"{draft_case.ordinal:02d}: rejected")
            continue
        case_id = f"research-qa-{provenance.category}-{next_number:03d}"
        review_values = {"by": marks.by, "on": marks.on, "read": draft_case.ordinal in marks.marked}
        review = {key: review_values[key] for key in review_keys}
        record = {key: (case_id if key == "id" else review if key == "review" else draft_case.value[key])
                  for key in synthetic.CASE_KEYS}
        findings = evalset.case_findings(record, relative, len(existing_cases) + len(added) + 1, known_courts)
        if findings:
            raise IntakeError("validate", f"ordinal {draft_case.ordinal:02d}: case line failed the eval-set shape", "Draft this case again and have the CSA review it, then retry.")
        sources = {entry["source_id"] for entry in cast(list[dict[str, object]], record["gold"])}
        if sources & cased:
            raise IntakeError("set", f"ordinal {draft_case.ordinal:02d}: gold document is already cased", "Reject that ordinal or draft uncased documents, then retry.")
        cased.update(sources)
        added.append(synthetic.case_line(record).encode("ascii"))
        next_number += 1
        label = synthetic.CLOSED_BOOK_LABEL in cast(list[str], record["labels"])
        labelled += int(label)
        disposition = "accepted" if draft_case.ordinal in marks.marked else "admitted unread"
        rows.append(f"{draft_case.ordinal:02d}: {disposition} {case_id}" + (f" {synthetic.CLOSED_BOOK_LABEL}" if label else ""))
    payload = (previous or b"") + b"".join(added)
    if added and not dry_run:
        try:
            write_atomic(path, payload)
        except OSError as error:
            raise IntakeError("write", "cases cannot be appended", _APPEND_FIX) from error
        verified = False
        try:
            reloaded = evalset.load_set(target, known_courts)
            verified = reloaded.ok
        finally:
            if not verified:
                try:
                    if previous is None:
                        path.unlink()
                    else:
                        write_atomic(path, previous)
                except OSError as error:
                    raise IntakeError("write", "set verification failed and previous cases could not be restored", _SET_FIX) from error
        if not verified:
            raise IntakeError("validate", "appended cases failed the eval-set reload; previous cases restored", "Correct the draft or set and retry the intake.")
    for row in rows:
        print(row, file=stdout)
    print(f"cases: {len(existing_cases) + len(added)} lines sha256 {hashlib.sha256(payload).hexdigest()}", file=stdout)
    rejected = len(marks.marked - marks.accepted)
    print(f"summary: accepted {len(marks.accepted)}, admitted unread {len(cases) - len(marks.marked)}, "
          f"rejected {rejected}, closed-book-answerable {labelled}", file=stdout)
    print(f"read: {len(marks.marked)} of {len(cases)} cases (share 1/{provenance.read_share}), rejected {rejected}", file=stdout)
    print(f"set digest: {'unchanged' if loaded.digest == provenance.set_digest else 'moved'} since the draft", file=stdout)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Intake CSA-reviewed synthetic cases")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("marks", type=Path, nargs="?")
    parser.add_argument("--template", action="store_true", help="print the marks template without writing files")
    parser.add_argument("--set", dest="set_root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, *, court_ids: Iterable[str] | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Read one complete CSA review and return the intake command status."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.template and args.marks is not None:
            parser.error("marks must be omitted with --template")
        if not args.template and args.marks is None:
            parser.error("marks are required unless --template is used")
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        if args.template:
            provenance = _load_manifest(args.manifest)
            print(manifest.marks_template(provenance.sheet_sha256, provenance.read_ordinals), end="", file=output)
        else:
            intake(args.manifest, args.marks, set_root=args.set_root, dry_run=args.dry_run, stdout=output, court_ids=court_ids)
    except IntakeError as error:
        print(f"{error.stage}: refused — {error.problem}. Fix: {error.fix}", file=errors)
        return 1
    except Exception as error:  # noqa: BLE001 - generated text never enters diagnostics
        print(f"synthetic intake: refused — internal error ({type(error).__name__}). Fix: Repair the intake command, then retry.", file=errors)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
