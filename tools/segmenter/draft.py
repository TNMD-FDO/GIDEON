"""Draft review sheets for unsigned segmenter cases on the box."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, TextIO, cast

from gideon.evaluation import evalset, record, segmenter_draft
from gideon.host import backuplock, cas, engine, nogpu, report, site, stack
from gideon.host.render.worker import SECTION_TYPES
from gideon.host.report import Problem
from gideon.host.sysio import BytesHost, LockingHost, PathLike, RealHost
from tools.segmenter import manifest
from tools.synthetic.manifest import original_link

DEFAULT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RENDERED_DIR: Final[Path] = Path("/etc/gideon/rendered")
SITE_PATH: Final[Path] = Path("/etc/gideon/site.yaml")
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
DOCUMENTS_SQL: Final[str] = """SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'opinion_id', o.opinion_id,
    'doc_id', d.doc_id,
    'text_source', d.text_source,
    'canonical_sha256', d.canonical_text_sha256,
    'reporter_cites', o.reporter_cites
)), '[]'::jsonb)::text
FROM opinions o
JOIN documents d ON d.doc_id = o.doc_id AND d.status = 'ready'
WHERE o.opinion_id IN (
    SELECT value::bigint FROM jsonb_array_elements_text(:'opinion_ids'::jsonb) AS value
);
"""
_SET_FIX: Final[str] = "Restore the evaluation set, then retry."
_OUT_FIX: Final[str] = "Choose an empty output directory outside the checkout, then retry."


class DraftHost(BytesHost, LockingHost, Protocol):
    """The box's record, store, and lock operations used by this command."""


class DraftError(Exception):
    """One content-free stage refusal and its corrective action."""

    def __init__(self, stage: str, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.stage = stage
        self.problem = problem
        self.fix = fix


@dataclass(frozen=True, slots=True)
class ReadyDocument:
    """The record facts needed to fetch a canonical text."""

    opinion_id: int
    doc_id: str
    canonical_sha256: str
    reporter_cites: tuple[str, ...]


def _selected(set_root: Path, requested: Sequence[str]) -> tuple[evalset.LoadedSet, tuple[str, ...]]:
    result = evalset.load_set(set_root)
    if result.loaded is None or result.findings:
        raise DraftError("set", "evaluation set cannot be loaded", _SET_FIX)
    loaded = result.loaded
    selection = evalset.select_cases(loaded, "segmenter-300") if "segmenter-300" in loaded.slices else None
    if selection is None:
        raise DraftError("set", "segmenter slice is absent", _SET_FIX)
    unsigned = set(selection.unsigned)
    if requested:
        for case_id in requested:
            if evalset.SEGMENTER_ID_PATTERN.fullmatch(case_id) is None:
                raise DraftError("set", "requested case id is invalid", "Name an unsigned segmenter case id, then retry.")
            if case_id not in unsigned:
                raise DraftError("set", f"case {case_id} is not unsigned in the slice", "Name an unsigned case in this set, then retry.")
        ids = tuple(sorted(set(requested)))
    else:
        ids = tuple(sorted(unsigned))
    if not ids:
        raise DraftError("set", "there is no unsigned segmenter case to draft", "Draw cases or select unsigned cases, then retry.")
    return loaded, ids


def check_output(path: Path, checkout: Path) -> None:
    """Refuse an output outside an empty directory or inside the checkout."""

    try:
        resolved = path.resolve()
        root = checkout.resolve()
    except (OSError, RuntimeError) as error:
        raise DraftError("write", "output path cannot be resolved", _OUT_FIX) from error
    if resolved == root or root in resolved.parents:
        raise DraftError("write", "output is inside the checkout", _OUT_FIX)
    if path.exists():
        if not path.is_dir():
            raise DraftError("write", "output is not an empty directory", _OUT_FIX)
        try:
            if any(path.iterdir()):
                raise DraftError("write", "output directory is not empty", _OUT_FIX)
        except OSError as error:
            raise DraftError("write", "output directory cannot be read", _OUT_FIX) from error


def _documents(io: DraftHost, rendered_dir: PathLike, opinion_ids: Sequence[int]) -> Mapping[int, ReadyDocument]:
    ids = json.dumps(sorted(set(opinion_ids)))
    sql = f"\\set opinion_ids '{ids}'\n{DOCUMENTS_SQL}"
    fix = f"Run {report.command('apply')}, then {stack.logs_fix(rendered_dir, record.POSTGRES_SERVICE)} and retry."
    try:
        result = io.run(record.reader_argv(rendered_dir), input=sql)
    except (OSError, subprocess.SubprocessError) as error:
        raise DraftError("resolve", "record read could not run", fix) from error
    if result.returncode != 0:
        raise DraftError("resolve", f"record read exited {result.returncode}", fix)
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise DraftError("resolve", "record reply is not JSON", fix) from error
    if not isinstance(value, list):
        raise DraftError("resolve", "record reply is not a list", fix)
    documents: dict[int, ReadyDocument] = {}
    requested = set(opinion_ids)
    for row in value:
        if not isinstance(row, dict) or set(row) != {"opinion_id", "doc_id", "text_source", "canonical_sha256", "reporter_cites"}:
            raise DraftError("resolve", "record reply has invalid document fields", fix)
        opinion_id, doc_id, source, sha256, cites = (
            row["opinion_id"], row["doc_id"], row["text_source"], row["canonical_sha256"],
            row["reporter_cites"] if row["reporter_cites"] is not None else [],
        )
        if (
            type(opinion_id) is not int or opinion_id not in requested or opinion_id in documents
            or not isinstance(doc_id, str) or _SHA256.fullmatch(doc_id) is None
            or source not in evalset.SEGMENTER_TEXT_SOURCES
            or not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None
            or not isinstance(cites, list) or not all(isinstance(cite, str) for cite in cites)
        ):
            raise DraftError("resolve", "record reply has invalid document facts", fix)
        documents[opinion_id] = ReadyDocument(opinion_id, doc_id, sha256, tuple(cites))
    return documents


def _canonical_text(io: DraftHost, case_id: str, document: ReadyDocument) -> str:
    value = cas.get(io, document.canonical_sha256)
    if isinstance(value, Problem):
        raise DraftError("text", f"case {case_id} canonical text is unavailable", value.fix)
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DraftError("text", f"case {case_id} canonical text is not UTF-8", "Restore the canonical object, then retry.") from error
    if not segmenter_draft.paragraph_table(text).starts:
        raise DraftError("text", f"case {case_id} canonical text has no paragraphs", "Re-ingest the named opinion, then retry.")
    return text


def _paragraphs(text: str, starts: Sequence[int]) -> tuple[str, ...]:
    return tuple(
        text[start:(starts[index + 1] if index + 1 < len(starts) else len(text))].strip()
        for index, start in enumerate(starts)
    )


def _sheet(case_id: str, opinion_id: int, court: str, sha256: str, cites: Sequence[str], text: str,
           paragraphs: Sequence[str], result: segmenter_draft.Draft) -> bytes:
    status = "drafted" if result.failure is None else "failed"
    marks = (
        {transition.paragraph: transition.section_type for transition in result.transitions}
        if result.transitions is not None else {0: "unknown"}
    )
    lines = [
        manifest.sheet_header(case_id, opinion_id, court, sha256, len(text), len(paragraphs), status),
        "Correct the bracketed mark where a section begins: replace a wrong mark with -,",
        "and a - with the missing mark. A line marked - continues the section above.",
        "The first line must always carry a bracketed mark.",
        "Available marks: " + ", ".join(SECTION_TYPES),
        "Read the companion full text before signing; only checked marks become labels.",
        "Each footnote starts its own section. A record holding one separate opinion alone",
        "(a dissent filed as its own opinion) carries that type from 0000.",
        "Cited as: " + ("; ".join(cites) if cites else "no cite recorded"),
        "Original: " + original_link(cites),
        "The original keeps the footnote numbers this text lost; check closing paragraphs there.",
        manifest.SHEET_PARAGRAPHS_HEADER,
    ]
    for index, paragraph in enumerate(paragraphs):
        preview = " ".join(paragraph.split())[:160]
        mark = f"[{marks[index]}]" if index in marks else "-"
        lines.append(f"{index:04d} {mark} {preview}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _full(paragraphs: Sequence[str]) -> bytes:
    return ("\n\n".join(f"{index:04d}\t{paragraph}" for index, paragraph in enumerate(paragraphs)) + "\n").encode("utf-8")


def _run(io: DraftHost, *, checkout: Path, set_root: Path, out: Path,
         requested: Sequence[str], rendered_dir: PathLike, site_path: PathLike,
         models_path: PathLike, now: Callable[[], datetime], sleep: Callable[[float], None],
         stdout: TextIO) -> None:
    if io.geteuid() != 0:
        raise DraftError("set", "root privileges are required", "Run the draft command with sudo, then retry.")
    loaded, case_ids = _selected(set_root, requested)
    try:
        slice_pins = manifest.pin_slice(set_root)
    except OSError as error:
        raise DraftError("set", "segmenter slice's files cannot be read", _SET_FIX) from error
    check_output(out, checkout)
    claim = backuplock.claim(
        io, command="tools.segmenter.draft", now=now(), lock=backuplock.ENGINE_LOCK,
    )
    if claim.refusal is not None:
        raise DraftError("lock", claim.refusal.detail, claim.refusal.fix)
    try:
        if nogpu.is_no_gpu_host(io):
            raise DraftError("engine", "the no-GPU marker refuses the draft", "Run the draft on the GPU box, then retry.")
        site_result = site.load_site(Path(site_path), host=io)
        if site_result.errors or site_result.config is None:
            fix = site_result.errors[0].fix if site_result.errors else "Restore the site file, then retry."
            raise DraftError("engine", "site file cannot be loaded", fix)
        target = engine.resolve_engine_target(
            io, rendered_dir, hardware_profile=site_result.config.hardware_profile,
            models_path=models_path, sleep=sleep,
        )
        if isinstance(target, Problem):
            raise DraftError("engine", target.problem, target.fix)
        opinion_ids = tuple(cast(int, loaded.cases_by_id[case_id]["opinion_id"]) for case_id in case_ids)
        documents = _documents(io, rendered_dir, opinion_ids)
        prompt = segmenter_draft.PROMPTS["segmenter-label@1"]
        entries: list[dict[str, object]] = []
        failed: Counter[str] = Counter()
        skipped = 0
        engine_seconds = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        try:
            # Run under sudo, the sheets go to whoever owns where they are
            # written, the person who corrects them.
            owner = manifest.owner_of(out)
            manifest.make_directory(out, owner)
        except OSError as error:
            raise DraftError("write", "output directory cannot be created", _OUT_FIX) from error
        for case_id, opinion_id in zip(case_ids, opinion_ids, strict=True):
            document = documents.get(opinion_id)
            if document is None:
                skipped += 1
                print(f"{case_id}: skipped not-ingested", file=stdout)
                continue
            text = _canonical_text(io, case_id, document)
            table = segmenter_draft.paragraph_table(text)
            paragraphs = _paragraphs(text, table.starts)
            result = segmenter_draft.draft(
                io, rendered_dir, served_model_name=target.served_model_name, text=text,
            )
            if result.elapsed_seconds is not None:
                engine_seconds += result.elapsed_seconds
            if result.prompt_tokens is not None:
                prompt_tokens += result.prompt_tokens
            if result.completion_tokens is not None:
                completion_tokens += result.completion_tokens
            status = "drafted" if result.failure is None else "failed"
            failure_code = None if result.failure is None else result.failure.code
            if failure_code is not None:
                failed[failure_code] += 1
            case = loaded.cases_by_id[case_id]
            court = case["court"]
            assert isinstance(court, str)
            sheet = _sheet(
                case_id, opinion_id, court, document.canonical_sha256, document.reporter_cites,
                text, paragraphs, result,
            )
            full = _full(paragraphs)
            try:
                manifest.write_atomic(out / f"{case_id}.txt", sheet, owner=owner)
                manifest.write_atomic(out / f"{case_id}.full.txt", full, owner=owner)
            except OSError as error:
                raise DraftError("write", f"case {case_id} sheet cannot be written", _OUT_FIX) from error
            entries.append(dict(zip(manifest.CASE_KEYS, (
                case_id, opinion_id, document.doc_id, document.canonical_sha256,
                table.length, list(table.starts), status, failure_code,
                hashlib.sha256(sheet).hexdigest(),
            ), strict=True)))
            outcome = f"{len(result.transitions)} transitions" if result.transitions is not None else f"failed {failure_code}"
            seconds = result.elapsed_seconds if result.elapsed_seconds is not None else 0.0
            print(f"{case_id}: {len(table.starts)} paragraphs, {result.windows} windows, {outcome}, {seconds:.3f} s", file=stdout)
        value = dict(zip(manifest.MANIFEST_KEYS, (
            manifest.MANIFEST_FORMAT, str(set_root.resolve()), loaded.digest, slice_pins,
            prompt.id, segmenter_draft.PROMPT_DIGESTS[prompt.id], entries,
        ), strict=True))
        try:
            manifest.write_atomic(
                out / "manifest.json",
                (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8"),
                owner=owner,
            )
        except OSError as error:
            raise DraftError("write", "manifest cannot be written", _OUT_FIX) from error
        figures = ", ".join(f"{code}={failed[code]}" for code in sorted(failed)) or "none"
        print(
            f"summary: drafted {len(entries) - sum(failed.values())}, failed {sum(failed.values())} ({figures}), "
            f"not-ingested {skipped}, engine {engine_seconds:.3f} s, "
            f"prompt tokens {prompt_tokens}, completion tokens {completion_tokens}",
            file=stdout,
        )
    finally:
        backuplock.release_claim(io, claim, lock=backuplock.ENGINE_LOCK)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draft segmenter review sheets")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--set", dest="set_root", type=Path)
    parser.add_argument("--case", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None, *, host: DraftHost | None = None,
         root: Path | None = None, rendered_dir: PathLike = RENDERED_DIR,
         site_path: PathLike = SITE_PATH, models_path: PathLike | None = None,
         clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Run the on-box draft stages and return the command exit code."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    checkout = DEFAULT_ROOT if root is None else root
    io = cast(DraftHost, RealHost()) if host is None else host
    try:
        _run(
            io, checkout=checkout,
            set_root=checkout / evalset.SET_ROOT if args.set_root is None else args.set_root,
            out=args.out, requested=args.case, rendered_dir=rendered_dir,
            site_path=site_path, models_path=checkout / "models.lock" if models_path is None else models_path,
            now=(lambda: datetime.now(UTC)) if clock is None else clock,
            sleep=sleep, stdout=output,
        )
    except DraftError as error:
        print(f"{error.stage}: refused — {error.problem}. Fix: {error.fix}", file=errors)
        return 1
    except Exception as error:  # noqa: BLE001 - opinions and replies never enter diagnostics
        print(f"segmenter draft: internal error ({type(error).__name__})", file=errors)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
