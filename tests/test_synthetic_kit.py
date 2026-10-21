"""Synthetic draft sheets and content-free provenance over fictitious corpus data."""

import ast
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from test_evaluation_record import read_psql_set
from test_evaluation_set import _synthetic_case
from test_index_record import row_for
from test_segmenter_kit import DraftHost

from gideon.evaluation import evalset, record, synthetic
from gideon.host import backuplock, courts, engine, site, worker
from gideon.host.index import sample as indexsample
from gideon.host.render.ci import CI_ROOT
from gideon.host.render.worker import (
    PATTERN_SET_ID,
    PIPELINE_VERSION,
    PRECEDENTIAL_VALUES,
)
from gideon.host.report import Problem
from gideon.host.sysio import PathLike
from gideon.worker import treatment
from gideon.worker.citations import NewCitation
from tools.segmenter import manifest as segmenter_manifest
from tools.synthetic import draft, intake, manifest

QUESTION = "FICTITIOUS PRIVATE QUESTION SENTINEL"
ANSWER = "FICTITIOUS PRIVATE REFERENCE SENTINEL"
CLOSED = "FICTITIOUS PRIVATE CLOSED-BOOK SENTINEL"
REASON = "FICTITIOUS PRIVATE READING SENTINEL"
CASE_NAME = "Fictitious Petitioner v. Imaginary Respondent"
PASSAGE = (
    "In this fictitious opinion, the court required written notice before the hearing. "
    "The invented judgment is vacated and remanded for that notice."
)
RENDERED = "/tmp/fictitious-production"
NOW = datetime(2099, 1, 4, tzinfo=UTC)


def _reply(value: object, *, finish: str = "stop", status: int = 200) -> engine.EngineReply:
    document = {
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(value)}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    return engine.EngineReply(status, json.dumps(document), document, (), True, None, 1.5, None)


def _proposal(passage: str = PASSAGE) -> engine.EngineReply:
    return _reply({"question": QUESTION, "answer": ANSWER, "passage": passage})


def _verdict(score: int = 3) -> engine.EngineReply:
    return _reply({"score": score, "reason": REASON, "failure_modes": []})


class SyntheticHost(DraftHost):
    """Serve sibling generations, production facts, store objects, and scripted replies."""

    def __init__(self, count: int = 2) -> None:
        super().__init__(PASSAGE)
        base = row_for()
        stamp = base.lexical_built_at
        self.generations = [replace(
            base, gen_id=indexsample.sample_name(base.gen_id), state="built",
            dense_built_at=stamp, built_at=stamp,
        )]
        self.documents = []
        self.manifest_rows: list[dict[str, str]] = []
        self.texts: dict[int, str] = {}
        self.objects = {}
        for index in range(count):
            opinion_id = index + 101
            text = f"Fictitious opinion {opinion_id}\n\n{PASSAGE}\n\nFictitious closing."
            text = text.ljust(synthetic.DOCUMENT_MIN_CHARS, " ")
            sha256 = hashlib.sha256(text.encode()).hexdigest()
            doc_id = hashlib.sha256(f"fictitious document {opinion_id}".encode()).hexdigest()
            self.texts[opinion_id] = text
            self.objects[sha256] = text.encode()
            self.documents.append({
                "opinion_id": opinion_id, "doc_id": doc_id, "source": "fictitious",
                "status": "ready", "canonical_sha256": sha256,
                "court": f"court{index:03d}", "decided_date": "2005-03-04",
                "reporter_cites": ["1959 U.S. App. LEXIS 4083", "265 F.2d 521"],
                "precedential": "published", "case_name": CASE_NAME,
            })
            self.manifest_rows.append({
                "source_id": indexsample.source_id("fictitious", opinion_id),
                "canonical_text_sha256": sha256, "doc_id": doc_id,
            })
        self.bodies: list[Mapping[str, object]] = []
        self.replies: list[engine.EngineReply | BaseException] = []
        self.fetched: list[str] = []
        self.responses: dict[str, str] = {}
        self.exit_codes: dict[str, int] = {}
        self.run_errors: dict[str, OSError | subprocess.SubprocessError] = {}
        self.present_courts = [str(row["court"]) for row in self.documents]
        self.case_names = [CASE_NAME]
        mapped = {court: courts.Court(court, None, None, "district", f"Fictitious {court}")
                  for court in self.present_courts}
        mapped.update({court: courts.Court(court, None, None, "circuit", name) for court, name in (
            ("exampleca", "Fictitious First Circuit"), ("examplecb", "Fictitious Second Circuit"),
        )})
        self.court_map = courts.CourtMap(
            courts.CourtSource("courts-2099-01-02.csv.bz2", "2099-01-02", "a" * 64, len(mapped), {}), mapped,
        )

    def set_text(self, index: int, text: str) -> None:
        """Replace one fictitious canonical object and its matching record hashes."""

        opinion_id = cast(int, self.documents[index]["opinion_id"])
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.texts[opinion_id] = text
        self.objects[digest] = text.encode()
        self.documents[index]["canonical_sha256"] = digest
        self.manifest_rows[index]["canonical_text_sha256"] = digest

    def run(
        self, argv: tuple[str, ...] | list[str], *, check: bool = False,
        input: str | None = None, cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        self.calls.append((tuple(argv), input))
        assert input is not None
        if "public.index_generations" in input:
            kind = "generations"
            assert list(argv) == worker.psql_argv(CI_ROOT)
            output = "\n".join(json.dumps(asdict(row)) for row in self.generations)
        elif "public.generation_documents" in input:
            kind = "manifest"
            assert list(argv) == worker.psql_argv(CI_ROOT)
            output = "\n".join(json.dumps(row) for row in self.manifest_rows)
        elif "SELECT DISTINCT court FROM opinions" in input:
            kind = "courts"
            assert list(argv) == record.reader_argv(RENDERED)
            output = json.dumps(sorted(set(self.present_courts)))
        elif "jsonb_array_elements(:'premises'::jsonb)" in input:
            kind = "premises"
            assert list(argv) == record.reader_argv(RENDERED)
            pairs = json.loads(read_psql_set(input.splitlines()[0], "premises"))
            output = json.dumps([pair for pair in pairs if any(
                all(surname.casefold() in name.casefold() for surname in pair) for name in self.case_names
            )])
        else:
            kind = "facts"
            assert list(argv) == record.reader_argv(RENDERED)
            requested = json.loads(input.split("'", 2)[1])
            output = json.dumps([row for row in self.documents if row["opinion_id"] in requested])
        if kind in self.run_errors:
            raise self.run_errors[kind]
        return subprocess.CompletedProcess(list(argv), self.exit_codes.get(kind, 0), self.responses.get(kind, output), REASON)

    def read_bytes(self, path: PathLike) -> bytes:
        self.fetched.append(Path(path).name)
        return super().read_bytes(path)

    def engine_call(self, *_args: object, **kwargs: object) -> engine.EngineReply:
        assert self.claimed and not self.released
        assert _args[1] == RENDERED
        body = kwargs["body"]
        assert isinstance(body, Mapping)
        self.bodies.append(body)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        response_format = cast(dict[str, object], body["response_format"])
        schema = cast(dict[str, object], response_format["json_schema"])
        if schema["name"] == "synthetic_treatment":
            return _reply({"question": QUESTION, "answer": ANSWER})
        if str(schema["name"]).startswith("synthetic_"):
            return _proposal()
        if schema["name"] == "closed_book_answer":
            return _reply({"answer": CLOSED})
        return _verdict()


TREATMENT_SENTENCE = (
    "The fictitious court overruled Fable v. Example, 123 F.3d 456, on the invented "
    "notice issue because the earlier rule ignored written notice."
)


class TreatmentHost(SyntheticHost):
    """Serve fictional pattern edges and the complete recorded spans they use."""

    def __init__(self, count: int = 3) -> None:
        super().__init__(count)
        self.edge_rows: list[dict[str, object]] = []
        self.section_rows: dict[str, list[dict[str, object]]] = {}
        self.citation_rows: dict[str, list[dict[str, object]]] = {}
        for index, fact in enumerate(self.documents):
            opinion = cast(int, fact["opinion_id"])
            doc_id = cast(str, fact["doc_id"])
            text = ("Fictitious heading.\n\n" + TREATMENT_SENTENCE + "\n\nFictitious later paragraph.").ljust(synthetic.DOCUMENT_MIN_CHARS, ".")
            digest = hashlib.sha256(text.encode()).hexdigest()
            self.texts[opinion] = text
            self.objects[digest] = text.encode()
            fact["canonical_sha256"] = digest
            self.manifest_rows[index]["canonical_text_sha256"] = digest
            citation_id = hashlib.sha256(f"fictitious edge {opinion}".encode()).hexdigest()
            section_id = hashlib.sha256(f"fictitious section {opinion}".encode()).hexdigest()
            standing = "dissent" if index % 3 == 2 else "majority"
            start = text.index("Fable v. Example")
            end = start + len("Fable v. Example, 123 F.3d 456")
            cluster = 1000 + index
            citation = NewCitation(
                citation_id, doc_id, 0, start, end, section_id, standing,
                "case_cite", "full", text[start:end], "123 F.3d 456", None, None,
                cluster, "fictitious/edges@1",
            )
            self.section_rows[doc_id] = [{
                "section_id": section_id, "section_type": standing, "char_start": 0, "char_end": len(text),
            }]
            self.citation_rows[doc_id] = [asdict(citation)]
            state = ("negative", "caution", None)[index % 3]
            self.edge_rows.append({
                "opinion_id": opinion, "doc_id": doc_id, "court": fact["court"],
                "decided_date": fact["decided_date"], "reporter_cites": fact["reporter_cites"], "case_name": fact["case_name"],
                "citation_id": citation_id, "signal_id": hashlib.sha256(f"fictitious signal {opinion}".encode()).hexdigest(),
                "pattern_set": PATTERN_SET_ID, "signal": "overruled", "qualifier": "none", "standing": standing,
                "state": state, "no_state_reason": "non_holding" if state is None else None,
                "verb_start": text.index("overruled"), "verb_end": text.index("overruled") + len("overruled"),
                "citation_start": start, "citation_end": end, "section_id": section_id, "section_type": standing,
                "cluster": cluster, "cluster_state": state, "listed": False,
                "cited": {"opinion_id": 900 + index, "court": "fictionalcourt", "decided_date": "1990-01-01",
                          "reporter_cites": ["123 F.3d 456"], "case_name": "Fable v. Example"},
            })

    def run(
        self, argv: tuple[str, ...] | list[str], *, check: bool = False,
        input: str | None = None, cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if input is not None and any(token in input for token in ("AS treatment_edges", "AS section_rows", "AS citation_rows")):
            self.calls.append((tuple(argv), input))
            assert list(argv) == record.reader_argv(RENDERED)
            pipeline_line = next(line for line in input.splitlines() if line.startswith("\\set pipeline "))
            assert read_psql_set(pipeline_line, "pipeline") == PIPELINE_VERSION
            if "AS treatment_edges" in input:
                kind = "edges"
                lines = input.splitlines()
                requested = json.loads(read_psql_set(next(line for line in lines if line.startswith("\\set opinion_ids ")), "opinion_ids"))
                assert read_psql_set(next(line for line in lines if line.startswith("\\set pattern_set ")), "pattern_set") == PATTERN_SET_ID
                rows = [row for row in self.edge_rows if row["opinion_id"] in requested]
            else:
                kind = "sections" if "AS section_rows" in input else "citations"
                doc_id = read_psql_set(input.splitlines()[0], "doc_id")
                rows = (self.section_rows if kind == "sections" else self.citation_rows)[doc_id]
            if kind in self.run_errors:
                raise self.run_errors[kind]
            return subprocess.CompletedProcess(list(argv), self.exit_codes.get(kind, 0), self.responses.get(kind, json.dumps(rows)), REASON)
        return super().run(argv, check=check, input=input, cwd=cwd, env=env, timeout=timeout, passthrough=passthrough)


MAJORITY_PASSAGE = (
    "The fictitious majority held that written notice must precede the invented hearing. "
    "The court remanded this imaginary case so that proper notice could be given."
)
DISSENT_PASSAGE = (
    "The fictitious dissent would affirm because the invented parties already knew of the hearing. "
    "This separate opinion would hold that another written notice was unnecessary."
)
SYLLABUS_PASSAGE = (
    "This fictitious syllabus summarizes an imaginary dispute about written notice. "
    "The summary is supplied by the invented reporter rather than the deciding court."
)


class AttributionHost(SyntheticHost):
    """Serve section summaries and spans over visibly invented opinion text."""

    def __init__(self, count: int = 1, *, blocks: Sequence[tuple[str, str, int]] | None = None) -> None:
        super().__init__(count)
        if blocks is None:
            blocks = (
                ("syllabus", SYLLABUS_PASSAGE, 200),
                ("majority", MAJORITY_PASSAGE, synthetic.SECTION_MIN_CHARS + 100),
                ("dissent", DISSENT_PASSAGE, synthetic.SECTION_MIN_CHARS + 100),
                ("footnote", "Fictitious footnote, not an opinion section.", 100),
            )
        self.section_rows: dict[str, list[dict[str, object]]] = {}
        self.summary_rows: list[dict[str, object]] = []
        self.passages = {kind: passage for kind, passage, _ in blocks}
        for index, fact in enumerate(self.documents):
            doc_id = cast(str, fact["doc_id"])
            sections = []
            text = ""
            for ordinal, (kind, passage, length) in enumerate(blocks):
                block = (passage + "\n\nFictitious section discussion.").ljust(length - 2, ".") + "\n\n"
                sections.append({
                    "section_id": hashlib.sha256(f"fictitious section {doc_id} {ordinal}".encode()).hexdigest(),
                    "section_type": kind, "char_start": len(text), "char_end": len(text) + len(block),
                })
                text += block
            digest = hashlib.sha256(text.encode()).hexdigest()
            self.texts[cast(int, fact["opinion_id"])] = text
            self.objects[digest] = text.encode()
            fact["canonical_sha256"] = digest
            self.manifest_rows[index]["canonical_text_sha256"] = digest
            self.section_rows[doc_id] = sections
            self.summary_rows.append({
                "opinion_id": fact["opinion_id"],
                "sections": [[section["section_type"], cast(int, section["char_end"]) - cast(int, section["char_start"])]
                             for section in sections if section["section_type"] != "footnote"],
            })

    def run(
        self, argv: tuple[str, ...] | list[str], *, check: bool = False,
        input: str | None = None, cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if input is not None and any(token in input for token in ("AS section_summaries", "AS section_rows")):
            self.calls.append((tuple(argv), input))
            assert list(argv) == record.reader_argv(RENDERED)
            pipeline_line = next(line for line in input.splitlines() if line.startswith("\\set pipeline "))
            assert read_psql_set(pipeline_line, "pipeline") == PIPELINE_VERSION
            if "AS section_summaries" in input:
                kind = "summary"
                requested = json.loads(read_psql_set(input.splitlines()[0], "opinion_ids"))
                rows = [row for row in self.summary_rows if row["opinion_id"] in requested]
            else:
                kind = "sections"
                rows = self.section_rows[read_psql_set(input.splitlines()[0], "doc_id")]
            if kind in self.run_errors:
                raise self.run_errors[kind]
            return subprocess.CompletedProcess(list(argv), self.exit_codes.get(kind, 0), self.responses.get(kind, json.dumps(rows)), REASON)
        return super().run(argv, check=check, input=input, cwd=cwd, env=env, timeout=timeout, passthrough=passthrough)

    def engine_call(self, *args: object, **kwargs: object) -> engine.EngineReply:
        body = cast(dict[str, object], kwargs["body"])
        schema = cast(dict[str, dict[str, object]], body["response_format"])["json_schema"]
        if schema["name"] == "synthetic_section_attribution" and not self.replies:
            user = cast(list[dict[str, str]], body["messages"])[1]["content"]
            section_type = user.split("Section type: ", 1)[1].splitlines()[0]
            assert self.claimed and not self.released
            self.bodies.append(body)
            return _proposal(self.passages[section_type])
        return super().engine_call(*args, **kwargs)


class WindowHost(SyntheticHost):
    """Return document-specific engine replies, with the first window reversed."""

    def __init__(self, *, concurrent: bool) -> None:
        super().__init__(6)
        self.concurrent = concurrent
        self.local = threading.local()
        self.barrier = threading.Barrier(3)
        self.completed = [threading.Event() for _ in self.documents]
        self.finished: list[int] = []
        self.worker_threads: set[int] = set()
        self.on_head: Callable[[], None] = lambda: None

    def read_bytes(self, path: PathLike) -> bytes:
        self.local.index = next(index for index, row in enumerate(self.manifest_rows)
                                if row["canonical_text_sha256"] == Path(path).name)
        self.worker_threads.add(threading.get_ident())
        return super().read_bytes(path)

    def engine_call(self, *args: object, **kwargs: object) -> engine.EngineReply:
        index = self.local.index
        body = cast(Mapping[str, object], kwargs["body"])
        schema = cast(dict[str, dict[str, str]], body["response_format"])["json_schema"]["name"]
        reply = super().engine_call(*args, **kwargs)
        if schema.startswith("synthetic_"):
            if self.concurrent and index < 3:
                self.barrier.wait(timeout=5)
            if index == 1:
                reply = _proposal("x" * synthetic.PASSAGE_MIN_CHARS)
            else:
                return reply
        elif schema == "closed_book_answer":
            if index != 2:
                return reply
            if self.concurrent and not self.completed[1].wait(5):
                raise AssertionError("fictitious second document never completed")
            reply = _reply({}, finish="length")
        elif self.concurrent and index == 0:
            if not self.completed[2].wait(5):
                raise AssertionError("fictitious third document never completed")
            self.on_head()
        self.finished.append(index)
        self.completed[index].set()
        return reply


def _run(host: SyntheticHost, root: Path, out: Path, *, count: int | None = 2,
         category: str = "lookup", generation: str | None = None,
         site_result: site.SiteLoadResult | None = None, target: engine.EngineTarget | Problem | None = None,
         stdout: io.StringIO | None = None,
         court_result: courts.CourtMapLoadResult | None = None,
         window: int = 1,
         ) -> tuple[int, str, str]:
    output, errors = io.StringIO() if stdout is None else stdout, io.StringIO()
    set_root = root / "eval-v9"
    set_root.mkdir(exist_ok=True, parents=True)
    args = ["--category", category, "--seed", "17", "--out", str(out), "--set", str(set_root)]
    if count is not None:
        args.extend(("--count", str(count)))
    if generation is not None:
        args.extend(("--generation", generation))
    config = cast(site.SiteConfig, SimpleNamespace(hardware_profile="fixture-profile"))
    with (
        patch.object(synthetic, "DRAFT_WINDOW", window),
        patch.object(site, "load_site", return_value=site_result if site_result is not None else site.SiteLoadResult(config=config)),
        patch.object(engine, "resolve_engine_target", return_value=target if target is not None else engine.EngineTarget("fixture-profile", "fixture-model", 100_000)),
        patch.object(engine, "call_model_server", side_effect=host.engine_call),
        patch.object(courts, "load_court_map", return_value=court_result if court_result is not None else courts.CourtMapLoadResult(court_map=host.court_map)),
    ):
        code = draft.main(
            args, host=cast(draft.DraftHost, host), root=root / "checkout", rendered_dir=RENDERED,
            clock=lambda: NOW, sleep=lambda _seconds: None,
            stdout=output, stderr=errors,
        )
    return code, output.getvalue(), errors.getvalue()


class Draft(unittest.TestCase):
    """The draft keeps code-retraced cases, releasing the lock on every path."""

    def assert_private_absent(self, text: str) -> None:
        for private in (QUESTION, ANSWER, CLOSED, REASON, PASSAGE, CASE_NAME):
            self.assertNotIn(private, text)

    def assert_checkpoint(self, out: Path, kept: int) -> None:
        self.assertEqual({path.name for path in out.iterdir()},
                         {"drafts.jsonl", "sheet.txt", "manifest.json", "marks.template.txt"})
        value = json.loads((out / "manifest.json").read_bytes())
        self.assertEqual(tuple(value), manifest.MANIFEST_KEYS)
        self.assertEqual(value["format"], manifest.MANIFEST_FORMAT)
        self.assertEqual(len(value["entries"]), kept)
        sheet = (out / "sheet.txt").read_bytes()
        self.assertEqual(value["sheet_bytes"], len(sheet))
        self.assertEqual(value["sheet_sha256"], hashlib.sha256(sheet).hexdigest())
        lines = (out / "drafts.jsonl").read_bytes().splitlines(keepends=True)
        self.assertEqual(len(lines), kept)
        for ordinal, (entry, line) in enumerate(zip(value["entries"], lines, strict=True), 1):
            self.assertEqual(entry["ordinal"], ordinal)
            self.assertEqual(entry["draft_sha256"], hashlib.sha256(line).hexdigest())
        self.assertEqual(value["read_ordinals"], list(synthetic.sample_ordinals(kept, value["read_share"])))
        self.assertEqual((out / "marks.template.txt").read_text(),
                         manifest.marks_template(value["sheet_sha256"], value["read_ordinals"]))

    def test_concurrent_replies_preserve_serial_outputs_and_bounded_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            serial = WindowHost(concurrent=False)
            serial_result = _run(serial, root, root / "serial", count=5)
            self.assertEqual((serial_result[0], serial_result[2]), (0, ""), serial_result[1])
            host = WindowHost(concurrent=True)
            main_thread = threading.get_ident()
            submitted: list[synthetic.DrawDocument] = []
            consumed = 0
            original_submit = ThreadPoolExecutor.submit
            original_result = Future.result
            original_write = draft.write_atomic

            def submit(executor: ThreadPoolExecutor, function: Callable[..., object], *args: object, **kwargs: object) -> object:
                self.assertEqual(threading.get_ident(), main_thread)
                self.assertLess(len(submitted) - consumed, 3)
                submitted.append(cast(synthetic.DrawDocument, args[4]))
                return original_submit(executor, function, *args, **kwargs)

            def consume(future: Future[object], timeout: float | None = None) -> object:
                nonlocal consumed
                self.assertEqual(threading.get_ident(), main_thread)
                value = original_result(future, timeout)
                consumed += 1
                return value

            def write(path: Path, payload: bytes, *, owner: tuple[int, int] | None = None) -> None:
                self.assertEqual(threading.get_ident(), main_thread)
                original_write(path, payload, owner=owner)

            host.on_head = lambda: self.assertEqual(len(submitted), 3)
            with (
                patch.object(ThreadPoolExecutor, "submit", new=submit),
                patch.object(Future, "result", new=consume),
                patch.object(draft, "write_atomic", side_effect=write),
            ):
                result = _run(host, root, root / "window", count=5, window=3)
            self.assertEqual(result, serial_result)
            self.assertEqual(host.finished[:3], [1, 2, 0])
            self.assertEqual(len(submitted), len(host.documents))
            self.assertNotIn(main_thread, host.worker_threads)
            self.assertEqual(len(host.worker_threads), 3)
            for path in (root / "serial").iterdir():
                self.assertEqual((root / "window" / path.name).read_bytes(), path.read_bytes())
            self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])

    def test_window_waits_for_unconsumed_work_before_releasing_lock_on_every_exit(self) -> None:
        for scenario in ("count", "refusal", "exception", "interrupt", "write"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost(5)
                shutdown_started = threading.Event()
                refusal_ready = threading.Event()
                finished: list[int] = []
                original_document = draft._document
                original_shutdown = ThreadPoolExecutor.shutdown
                original_write = draft.write_atomic

                def work(*args: object, scenario: str = scenario, host: SyntheticHost = host,
                         shutdown_started: threading.Event = shutdown_started,
                         refusal_ready: threading.Event = refusal_ready,
                         finished: list[int] = finished,
                         original_document: Callable[..., object] = original_document) -> object:
                    document = cast(synthetic.DrawDocument, args[4])
                    if document.opinion_id >= 103 and not shutdown_started.wait(5):
                        raise AssertionError("executor never began waiting for pending work")
                    if document.opinion_id == 101 and scenario in {"refusal", "exception", "interrupt"} and not refusal_ready.wait(5):
                        raise AssertionError("later fictitious document never reached its refusal")
                    self.assertFalse(host.released)
                    value = original_document(*args)
                    finished.append(document.opinion_id)
                    if document.opinion_id == 102:
                        refusal_ready.set()
                        if scenario in {"count", "refusal"}:
                            raise draft.DraftError("engine", "fictitious worker refused", "Repair the fictitious worker, then retry.")
                        if scenario == "exception":
                            raise RuntimeError(REASON)
                        if scenario == "interrupt":
                            raise KeyboardInterrupt()
                    return value

                def shutdown(executor: ThreadPoolExecutor, wait: bool = True, *, cancel_futures: bool = False,
                             host: SyntheticHost = host, shutdown_started: threading.Event = shutdown_started,
                             original_shutdown: Callable[..., None] = original_shutdown) -> None:
                    self.assertTrue(wait)
                    self.assertFalse(host.released)
                    shutdown_started.set()
                    original_shutdown(executor, wait=wait, cancel_futures=cancel_futures)

                def write(path: Path, payload: bytes, *, owner: tuple[int, int] | None = None,
                          scenario: str = scenario, original_write: Callable[..., None] = original_write) -> None:
                    if scenario == "write" and path.name == "manifest.json" and json.loads(payload)["entries"]:
                        raise OSError(REASON)
                    original_write(path, payload, owner=owner)

                with (
                    patch.object(draft, "_document", side_effect=work),
                    patch.object(ThreadPoolExecutor, "shutdown", new=shutdown),
                    patch.object(draft, "write_atomic", side_effect=write),
                ):
                    if scenario == "interrupt":
                        with self.assertRaises(KeyboardInterrupt):
                            _run(host, root, root / "draft", count=3, window=3)
                    else:
                        code, output, errors = _run(host, root, root / "draft", count=1 if scenario == "count" else 3, window=3)
                        self.assertEqual(code, 0 if scenario == "count" else 1, output + errors)
                        if scenario == "count":
                            self.assertEqual(len(host.fetched), 3)
                            self.assertIn("engine 4.500 s, prompt tokens 33, completion tokens 21", output)
                            self.assert_checkpoint(root / "draft", 1)
                        elif scenario in {"refusal", "exception"}:
                            self.assertIn("01:", output)
                            self.assert_checkpoint(root / "draft", 1)
                            self.assertNotIn(REASON, errors)
                self.assertTrue(shutdown_started.is_set())
                self.assertIn(103, finished)
                self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])

    def test_four_outputs_coordinates_digests_owners_and_no_text_in_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "draft"
            host = SyntheticHost()
            owners = []
            original = segmenter_manifest.os.fchown

            def chown(descriptor: int, uid: int, gid: int) -> None:
                owners.append((uid, gid))
                original(descriptor, uid, gid)

            with patch.object(segmenter_manifest.os, "fchown", side_effect=chown):
                code, output, errors = _run(host, root, out)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
            self.assertEqual(len(host.claimed), 1)
            self.assertEqual(json.loads(host.claimed[0][1])["command"], "tools.synthetic.draft")
            self.assertEqual(len(host.calls), 3)
            self.assertEqual(len(host.bodies), 6)
            self.assertEqual({path.name for path in out.iterdir()}, {"sheet.txt", "drafts.jsonl", "marks.template.txt", "manifest.json"})
            self.assertEqual(owners, [manifest_owner := segmenter_manifest.owner_of(root)] * (4 * (len(host.documents) + 1)))
            for path in out.iterdir():
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), manifest_owner)
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            value = json.loads((out / "manifest.json").read_bytes())
            self.assertEqual(tuple(value), manifest.MANIFEST_KEYS)
            self.assertIsNone(value["case_pin"])
            loaded = evalset.load_set(root / "eval-v9").loaded
            assert loaded is not None
            self.assertEqual(value["set_digest"], loaded.digest)
            self.assertEqual(value["template_digest"], synthetic.TEMPLATE_DIGESTS[value["template"]])
            self.assertEqual(value["reading_digest"], synthetic.READING_DIGESTS[value["reading"]])
            lines = (out / "drafts.jsonl").read_bytes().splitlines(keepends=True)
            sheet = (out / "sheet.txt").read_bytes()
            self.assertEqual(value["sheet_sha256"], hashlib.sha256(sheet).hexdigest())
            self.assertEqual(value["sheet_bytes"], len(sheet))
            self.assertEqual(value["asked"], 2)
            self.assertEqual(value["seed"], 17)
            self.assertEqual(value["discards"], [])
            for entry, line in zip(value["entries"], lines, strict=True):
                self.assertEqual(tuple(entry), manifest.ENTRY_KEYS)
                self.assertEqual(entry["draft_sha256"], hashlib.sha256(line).hexdigest())
                case = json.loads(line)
                self.assertEqual(tuple(case), manifest.DRAFT_KEYS)
                gold = case["gold"][0]
                opinion_id = int(gold["source_id"].rsplit(":", 1)[1])
                self.assertEqual(host.texts[opinion_id][gold["start"]:gold["end"]], gold["quote"])
                self.assertEqual(gold["quote"], PASSAGE)
                self.assertEqual(entry["length"], len(gold["quote"]))
                self.assertEqual(entry["quote_sha256"], hashlib.sha256(gold["quote"].encode()).hexdigest())
                self.assertEqual(case["labels"], [synthetic.SYNTHETIC_ORIGIN, synthetic.CLOSED_BOOK_LABEL])
                self.assertEqual(case["closed_book_answer"], CLOSED)
                self.assertEqual(entry["ordinal"], case["ordinal"])
                complete = {key: (f"research-qa-lookup-{entry['ordinal']:03d}" if key == "id" else
                                  {"by": "CSA-1", "on": "2099-01-04"} if key == "review" else case[key])
                            for key in synthetic.CASE_KEYS}
                self.assertEqual(evalset.case_findings(complete, synthetic.case_path("lookup"), 1), ())
            marks = (out / "marks.template.txt").read_text()
            header, *ordinals = marks.splitlines()
            self.assertIn(value["sheet_sha256"][:manifest.DIGEST_PREFIX_CHARS], header)
            self.assertEqual(ordinals, ["01 ", "02 "])
            self.assertIsNotNone(manifest.MARKS_HEADER_PATTERN.fullmatch(header.replace("<CSA role id>", "CSA-1").replace("<ISO date>", "2099-01-04")))
            self.assertIn("Original: https://www.courtlistener.com/c/F.2d/265/521/", sheet.decode())
            self.assertIn(QUESTION, sheet.decode())
            self.assertIn(CLOSED, sheet.decode())
            self.assertIn("prompt tokens 66, completion tokens 42", output)
            self.assertIn("engine 9.000 s", output)
            self.assert_private_absent(output + errors + (out / "manifest.json").read_text())

    def test_newest_eligible_generation_and_explicit_generation(self) -> None:
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as directory:
                host = SyntheticHost()
                older = host.generations[0]
                newer = replace(older, gen_id=older.gen_id.replace("2099-01-03", "2099-01-04"), created_at="2099-01-04T11:00:00+00:00")
                ineligible = replace(newer, gen_id=newer.gen_id.removesuffix("__sample"), created_at="2099-01-05T11:00:00+00:00")
                host.generations = [newer, ineligible, older]
                root = Path(directory)
                code, output, errors = _run(host, root, root / "draft", generation=older.gen_id if explicit else None)
                self.assertEqual((code, errors), (0, ""), output)
                chosen = older if explicit else newer
                self.assertIn(f"generation: {chosen.gen_id}", output)
                self.assertIn(worker.bind("v_name", chosen.gen_id), host.calls[1][1] or "")

    def test_count_stops_the_walk_and_exhaustion_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = SyntheticHost(3)
            code, output, errors = _run(host, root, root / "one", count=1)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(len(host.fetched), 1)
            self.assertEqual(len(host.bodies), 3)
            host = SyntheticHost(1)
            code, output, errors = _run(host, root, root / "exhausted", count=2)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertIn("order: exhausted, kept 1 of 2", output)

    def test_count_defaults_to_each_category_planned_count(self) -> None:
        for category, count in synthetic.PLANNED_COUNTS.items():
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = TreatmentHost(1) if category == "treatment" else AttributionHost() if category == "section-attribution" else SyntheticHost(1)
                code, output, errors = _run(host, root, root / "draft", count=None, category=category)
                self.assertEqual((code, errors), (0, ""), output)
                value = json.loads((root / "draft/manifest.json").read_bytes())
                self.assertEqual(value["asked"], count)
                version = 2 if category in ("retrieval", "synthesis", "existence") else 1
                self.assertEqual(value["template"], f"{category}@{version}")
                self.assertIn(f"order: exhausted, kept 1 of {count}", output)

    def test_caption_slot_reaches_proposal_and_sheet_only(self) -> None:
        for category in ("lookup", "retrieval", "synthesis", "existence"):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost(1)
                host.documents[0]["case_name"] = f"  {CASE_NAME}  "
                out = root / "draft"
                code, output, errors = _run(host, root, out, count=1, category=category)
                self.assertEqual((code, errors), (0, ""), output)
                user = cast(list[dict[str, str]], host.bodies[0]["messages"])[1]["content"]
                if category == "lookup":
                    self.assertNotIn("Case name:", user)
                    self.assertNotIn(CASE_NAME, user)
                else:
                    self.assertIn(f"Reporter citations: {'; '.join(cast(list[str], host.documents[0]['reporter_cites']))}\nCase name: {CASE_NAME}\n", user)
                sheet = (out / "sheet.txt").read_text().splitlines()
                source = next(i for i, line in enumerate(sheet) if line.startswith("Source:"))
                self.assertEqual(sheet[source + 1], f"Case name: {CASE_NAME}")
                self.assert_private_absent(output + errors + (out / "manifest.json").read_text())
                facts = draft._facts(cast(draft.DraftHost, host), RENDERED, tuple(
                    (row["source_id"], row["canonical_text_sha256"], row["doc_id"]) for row in host.manifest_rows
                ))
                self.assertEqual(facts[101].case_name, host.documents[0]["case_name"])
                self.assertNotIn(CASE_NAME, repr(facts))

    def test_missing_case_names_skip_before_fetch_for_every_category(self) -> None:
        for category in synthetic.PLANNED_COUNTS:
            for case_name in (None, " \t "):
                with self.subTest(category=category, case_name=case_name), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    host = SyntheticHost(1)
                    host.documents[0]["case_name"] = case_name
                    out = root / "draft"
                    code, output, errors = _run(host, root, out, count=1, category=category)
                    self.assertEqual((code, errors), (0, ""), output)
                    value = json.loads((out / "manifest.json").read_bytes())
                    self.assertEqual((host.fetched, host.bodies), ([], []))
                    self.assert_checkpoint(out, 0)
                    source_id = host.manifest_rows[0]["source_id"]
                    self.assertIn(f"{source_id}: skipped no-case-name\n", output)
                    self.assertEqual(value["discards"], [{"reason": "no-case-name", "source_id": source_id, "edge": None, "kind": None}])

    def test_publication_and_case_name_floors_before_store_and_engine_requests(self) -> None:
        cases = tuple(
            ("precedential", value, "unpublished") for value in PRECEDENTIAL_VALUES if value != "published"
        ) + tuple(("case_name", value, "no-case-name") for value in (None, "", " \n\t "))
        for category in synthetic.PLANNED_COUNTS:
            for key, value, reason in cases:
                with self.subTest(category=category, key=key, value=value), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    host = SyntheticHost(1)
                    host.documents[0][key] = value
                    code, output, errors = _run(host, root, root / "draft", count=1, category=category)
                    self.assertEqual((code, errors), (0, ""), output)
                    self.assertEqual((host.fetched, host.bodies), ([], []))
                    source = host.manifest_rows[0]["source_id"]
                    self.assertIn(f"{source}: skipped {reason}\n", output)
                    self.assertIn(f"discarded 1 ({reason}=1)", output)
                    provenance = json.loads((root / "draft/manifest.json").read_bytes())
                    self.assertEqual(provenance["discards"], [{"reason": reason, "source_id": source, "edge": None, "kind": None}])
                    self.assert_private_absent(output + errors)

    def test_new_document_facts_are_required_typed_and_withheld_from_repr(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("precedential", None), ("precedential", "invented"), ("precedential", []),
            ("case_name", 1), ("case_name", True), ("case_name", []), ("case_name", {}),
        )
        for key, value in (*cases, ("precedential", "missing"), ("case_name", "missing")):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost(1)
                if value == "missing":
                    del host.documents[0][key]
                else:
                    host.documents[0][key] = value
                code, output, errors = _run(host, root, root / "draft", count=1)
                self.assertEqual((code, output), (1, ""))
                self.assertIn("facts: refused", errors)
                self.assertIn("Fix:", errors)
                self.assertEqual((host.fetched, host.bodies), ([], []))
                self.assert_private_absent(errors)
        host = SyntheticHost(1)
        host.documents[0]["case_name"] = QUESTION
        rows = tuple((row["source_id"], row["canonical_text_sha256"], row["doc_id"]) for row in host.manifest_rows)
        facts = draft._facts(cast(draft.DraftHost, host), RENDERED, rows)
        self.assertEqual(facts[101].case_name, QUESTION)
        self.assertEqual(facts[101].precedential, "published")
        self.assertNotIn(QUESTION, repr(facts[101]))
        sql = host.calls[-1][1]
        assert sql is not None
        self.assertIn("'precedential', o.precedential", sql)
        self.assertIn("'case_name', o.case_name", sql)

    def test_short_documents_cost_no_engine_call_and_floor_counts_code_points(self) -> None:
        for length in (synthetic.DOCUMENT_MIN_CHARS - 1, synthetic.DOCUMENT_MIN_CHARS):
            with self.subTest(length=length), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost(1)
                text = PASSAGE + "😀" * (length - len(PASSAGE))
                host.set_text(0, text)
                code, output, errors = _run(host, root, root / "draft", count=1)
                self.assertEqual((code, errors), (0, ""), output)
                self.assertEqual(len(host.fetched), 1)
                short = length < synthetic.DOCUMENT_MIN_CHARS
                self.assertEqual(len(host.bodies), 0 if short else 3)
                self.assert_checkpoint(root / "draft", 0 if short else 1)
                value = json.loads((root / "draft/manifest.json").read_bytes())
                if short:
                    self.assertIn(f"{host.manifest_rows[0]['source_id']}: discarded document-short", output)
                    self.assertIn("document-short=1", output)
                    self.assertEqual(value["discards"], [{"reason": "document-short", "source_id": host.manifest_rows[0]["source_id"], "edge": None, "kind": None}])
                else:
                    self.assertEqual(value["discards"], [])

    def test_sample_and_full_read_headers_ordinals_and_template_lines(self) -> None:
        for category in ("lookup", "retrieval"):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                out = root / "draft"
                host = SyntheticHost(synthetic.READ_SHARE + 2)
                code, output, errors = _run(host, root, out, count=len(host.documents), category=category)
                self.assertEqual((code, errors), (0, ""), output)
                self.assert_checkpoint(out, len(host.documents))
                value = json.loads((out / "manifest.json").read_bytes())
                share = 1 if category == "lookup" else synthetic.READ_SHARE
                self.assertEqual(value["read_share"], share)
                ordinals = synthetic.sample_ordinals(len(host.documents), share)
                self.assertEqual(value["read_ordinals"], list(ordinals))
                headers = tuple(line for line in (out / "sheet.txt").read_text().splitlines()
                                if line.startswith(manifest.SHEET_BLOCK_HEADER))
                self.assertEqual(headers, tuple(
                    f"Synthetic case {ordinal:02d} {'required' if ordinal in ordinals else 'optional'}"
                    for ordinal in range(1, len(host.documents) + 1)
                ))
                self.assertEqual((out / "marks.template.txt").read_text().splitlines()[1:],
                                 [f"{ordinal:02d} " for ordinal in ordinals])

    def test_checkpoints_exist_before_requests_and_survive_third_case_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "draft"
            host = SyntheticHost(3)
            host.replies = [_proposal(), _reply({"answer": CLOSED}), _verdict()] * 2 + [
                draft.DraftError("engine", "fictitious engine stopped", "Repair the fictitious engine, then retry."),
            ]
            original = host.engine_call
            snapshots: list[dict[str, bytes]] = []

            def call(*args: object, **kwargs: object) -> engine.EngineReply:
                if len(host.bodies) % 3 == 0:
                    self.assert_checkpoint(out, len(host.bodies) // 3)
                    snapshots.append({path.name: path.read_bytes() for path in out.iterdir()})
                return original(*args, **kwargs)

            with patch.object(host, "engine_call", side_effect=call):
                code, output, errors = _run(host, root, out, count=3, category="retrieval")
            self.assertEqual(code, 1)
            self.assertIn("engine: refused — fictitious engine stopped", errors)
            self.assertIn("Fix:", errors)
            self.assertEqual(len(snapshots), 3)
            self.assert_checkpoint(out, 2)
            self.assertEqual({path.name: path.read_bytes() for path in out.iterdir()}, snapshots[-1])
            for previous, current in zip(snapshots, snapshots[1:], strict=False):
                for name in ("drafts.jsonl", "sheet.txt"):
                    self.assertTrue(current[name].startswith(previous[name]))
            value = json.loads((out / "manifest.json").read_bytes())
            self.assertEqual(value["asked"], 3)
            self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
            self.assert_private_absent(output + errors)

            marks_path = root / "marks.txt"
            marks_path.write_text((out / "marks.template.txt").read_text()
                                  .replace("<CSA role id>", "CSA-1").replace("<ISO date>", "2099-01-04")
                                  .replace(" \n", " accept\n"))
            result = _run_intake(out / "manifest.json", marks_path, tuple(str(row["court"]) for row in host.documents))
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            records = [json.loads(line) for line in (root / "eval-v9" / synthetic.case_path("retrieval")).read_bytes().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual([row["review"]["read"] for row in records], [True, False])

    def test_checkpoint_write_order_and_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "draft"
            host = SyntheticHost()
            original = draft.write_atomic
            writes = []

            def write(path: Path, payload: bytes, *, owner: tuple[int, int] | None = None) -> None:
                writes.append(path.name)
                self.assertEqual(owner, segmenter_manifest.owner_of(root))
                if len(writes) <= 4:
                    self.assertEqual(host.bodies, [])
                if path.name == "manifest.json":
                    value = json.loads(payload)
                    sheet = (out / "sheet.txt").read_bytes()
                    self.assertEqual(value["sheet_bytes"], len(sheet))
                    self.assertEqual(value["sheet_sha256"], hashlib.sha256(sheet).hexdigest())
                    self.assertEqual(len(value["entries"]), len((out / "drafts.jsonl").read_bytes().splitlines()))
                original(path, payload, owner=owner)

            with patch.object(draft, "write_atomic", side_effect=write):
                code, output, errors = _run(host, root, out)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(writes, ["drafts.jsonl", "sheet.txt", "manifest.json", "marks.template.txt"] * (len(host.documents) + 1))

    def test_every_stdout_row_is_flushed_before_the_next_request(self) -> None:
        class FlushedOutput(io.StringIO):
            def __init__(self) -> None:
                super().__init__()
                self.flushed = ""

            def flush(self) -> None:
                self.flushed = self.getvalue()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = SyntheticHost(5)
            host.documents[0]["status"] = "processing"
            host.set_text(1, PASSAGE)
            host.replies = [_reply({}, finish="length")]
            output = FlushedOutput()
            original = host.engine_call

            def call(*args: object, **kwargs: object) -> engine.EngineReply:
                self.assertEqual(output.flushed, output.getvalue())
                return original(*args, **kwargs)

            with patch.object(host, "engine_call", side_effect=call):
                code, text, errors = _run(host, root, root / "draft", count=3, stdout=output)
            self.assertEqual((code, errors), (0, ""), text)
            self.assertEqual(output.flushed, text)
            self.assertIn(": skipped not-ready", text)
            self.assertIn(": discarded document-short", text)
            self.assertIn(": discarded truncated", text)
            self.assertIn("order: exhausted", text)
            self.assertIn("summary:", text)

    def test_a_discard_does_not_use_a_kept_case_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = SyntheticHost(3)
            host.replies = [_reply({}, finish="length")]
            code, output, errors = _run(host, root, root / "draft", count=1)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(len(host.fetched), 2)
            self.assertEqual(len(host.bodies), 4)
            value = json.loads((root / "draft/manifest.json").read_bytes())
            self.assertEqual(len(value["entries"]), 1)
            self.assertEqual(value["entries"][0]["ordinal"], 1)
            self.assertEqual(value["entries"][0]["source_id"], host.manifest_rows[1]["source_id"])
            self.assertEqual(value["discards"], [{"reason": "truncated", "source_id": host.manifest_rows[0]["source_id"], "edge": None, "kind": None}])

    def test_empty_built_or_live_sample_reports_exhaustion_without_engine_calls(self) -> None:
        for state in ("built", "live"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost(0)
                host.generations = [replace(host.generations[0], state=state)]
                code, output, errors = _run(host, root, root / "draft", count=1)
                self.assertEqual((code, errors), (0, ""), output)
                self.assertIn("order: exhausted, kept 0 of 1", output)
                self.assertEqual(host.bodies, [])
                self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
                self.assertEqual((root / "draft/drafts.jsonl").read_bytes(), b"")
                self.assert_checkpoint(root / "draft", 0)
                value = json.loads((root / "draft/manifest.json").read_bytes())
                self.assertEqual(value["sheet_bytes"], 0)
                self.assertEqual(value["read_ordinals"], [])

    def test_skips_are_counted_before_any_store_or_engine_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = SyntheticHost(4)
            host.documents[0]["canonical_sha256"] = "a" * 64
            host.documents[1].update(status="processing", canonical_sha256=None)
            case = _synthetic_case()
            gold = cast(list[dict[str, object]], case["gold"])
            gold[0].update(source_id=host.manifest_rows[2]["source_id"], sha256=host.manifest_rows[2]["canonical_text_sha256"])
            case["cluster_id"] = gold[0]["source_id"]
            case["expected"] = {"answer": ANSWER, "must_cite": [gold[0]["source_id"]]}
            set_root = root / "eval-v9"
            set_root.mkdir()
            category_path = set_root / synthetic.case_path("lookup")
            category_path.parent.mkdir()
            category_path.write_text(synthetic.case_line(case))
            code, output, errors = _run(host, root, root / "draft", count=2)
            self.assertEqual((code, errors), (0, ""), output)
            value = json.loads((root / "draft/manifest.json").read_bytes())
            case_pin = manifest.pin_file(category_path)
            assert case_pin is not None
            self.assertEqual(value["case_pin"], list(case_pin))
            self.assertEqual([entry["reason"] for entry in value["discards"]], ["hash-changed", "not-ready", "already-cased"])
            self.assertEqual(len(host.fetched), 1)
            self.assertEqual(len(host.bodies), 3)
            self.assertIn("discarded 3", output)
            self.assert_private_absent(output + errors)

    def test_every_retrace_discard_is_counted(self) -> None:
        cases = (
            ("empty", "\n\t ", None),
            ("too-short", "short", None),
            ("too-long", "x" * (synthetic.PASSAGE_MAX_CHARS + 1), "raised-schema-bound"),
            ("not-found", "x" * synthetic.PASSAGE_MIN_CHARS, None),
            ("ambiguous", PASSAGE, "repeat"),
        )
        for reason, passage, tweak in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost(1)
                host.replies = [_proposal(passage)]
                if tweak == "repeat":
                    text = host.texts[101] + "\n\n" + PASSAGE
                    host.set_text(0, text)
                # The public retrace admits this reason; the proposal schema
                # normally catches the same excessive passage earlier.
                with patch.object(synthetic, "retrace", return_value=synthetic.Discard(reason)) if tweak == "raised-schema-bound" else contextlib.nullcontext():
                    if tweak == "raised-schema-bound":
                        host.replies = [_proposal()]
                    code, output, errors = _run(host, root, root / "draft", count=1)
                self.assertEqual((code, errors), (0, ""), output)
                self.assertIn(f"{reason}=1", output)
                self.assertEqual(len(host.bodies), 1)
                self.assertEqual((root / "draft/drafts.jsonl").read_bytes(), b"")
                value = json.loads((root / "draft/manifest.json").read_bytes())
                self.assertEqual(value["entries"], [])
                self.assertEqual(value["discards"][0]["reason"], reason)
                self.assert_private_absent(output + errors)

    def test_proposal_failures_and_failed_closed_book_runs_keep_no_case(self) -> None:
        cases = (
            ("request-failed", [engine.EngineReply(None, "", None, (), False, None, None, Problem(REASON, REASON))]),
            ("http-status", [_reply({}, status=503)]),
            ("truncated", [_reply({}, finish="length")]),
            ("off-schema", [_reply({"question": QUESTION})]),
            ("closed-book-failed", [_proposal(), _reply({}, status=503)]),
            ("closed-book-failed", [_proposal(), _reply({"answer": ""})]),
            ("closed-book-failed", [_proposal(), engine.EngineReply(None, "", None, (), False, None, None, Problem(REASON, REASON))]),
            ("closed-book-failed", [_proposal(), _reply({"answer": CLOSED}), _reply({}, finish="length")]),
            ("closed-book-failed", [_proposal(), _reply({"answer": CLOSED}), _reply({"score": 3, "reason": REASON})]),
        )
        for reason, replies in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost(1)
                host.replies.extend(replies)
                code, output, errors = _run(host, root, root / "draft", count=1)
                self.assertEqual((code, errors), (0, ""), output)
                self.assertIn(f"{reason}=1", output)
                self.assertEqual((root / "draft/drafts.jsonl").read_bytes(), b"")
                self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
                self.assert_private_absent(output + errors)

    def test_partial_closed_book_score_keeps_case_without_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = SyntheticHost(1)
            host.replies = [_proposal(), _reply({"answer": CLOSED}), _verdict(2)]
            code, output, errors = _run(host, root, root / "draft", count=1)
            self.assertEqual((code, errors), (0, ""), output)
            case = json.loads((root / "draft/drafts.jsonl").read_bytes())
            self.assertEqual(case["labels"], [synthetic.SYNTHETIC_ORIGIN])
            self.assertIn("closed-book-answerable 0", output)

    def test_unfinished_closed_book_answer_is_kept_without_reading_and_intakes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "draft"
            host = SyntheticHost(1)
            host.replies = [_proposal(), _reply({}, finish="length")]
            with patch.object(synthetic, "read_closed_book", wraps=synthetic.read_closed_book) as read:
                code, output, errors = _run(host, root, out, count=1)
            self.assertEqual((code, errors), (0, ""), output)
            read.assert_not_called()
            self.assertEqual(len(host.bodies), 2)
            self.assert_checkpoint(out, 1)
            value = json.loads((out / "manifest.json").read_bytes())
            self.assertEqual((value["entries"][0]["closed_book_score"], value["entries"][0]["label"]), (0, None))
            self.assertEqual(value["discards"], [])
            case = json.loads((out / "drafts.jsonl").read_bytes())
            self.assertEqual(case["closed_book_answer"], synthetic.CLOSED_BOOK_UNFINISHED)
            self.assertEqual(case["labels"], [synthetic.SYNTHETIC_ORIGIN])
            self.assertIn("engine 3.000 s, prompt tokens 22, completion tokens 14", output)
            marks = root / "marks.txt"
            marks.write_text((out / "marks.template.txt").read_text()
                             .replace("<CSA role id>", "CSA-1").replace("<ISO date>", "2099-01-04")
                             .replace(" \n", " accept\n"))
            result = _run_intake(out / "manifest.json", marks, (str(host.documents[0]["court"]),))
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            self.assertIn("summary: accepted 1, admitted unread 0, rejected 0, closed-book-answerable 0", result[1])

    def test_pre_run_refusals_have_fixes_no_rows_and_no_engine_calls(self) -> None:
        scenarios = (
            "root", "set", "category", "inside", "nonempty", "holder", "no-gpu", "site", "engine",
            "no-generation", "generation-name", "generation-exit", "generation-unavailable", "generation-json",
            "manifest-exit", "manifest-row", "source-id", "facts-exit", "facts-unavailable", "facts-json", "facts-row", "case-name-type",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost()
                out = root / "draft"
                category = "lookup"
                generation = None
                site_result = None
                target = None
                if scenario == "root":
                    host.root = False
                elif scenario == "set":
                    (root / "eval-v9/bad").mkdir(parents=True)
                    (root / "eval-v9/bad/cases.jsonl").write_text(QUESTION + "\n")
                elif scenario == "category":
                    category = "edition"
                elif scenario == "inside":
                    out = root / "checkout/draft"
                elif scenario == "nonempty":
                    out.mkdir()
                    (out / "existing").touch()
                elif scenario == "holder":
                    host.holder = backuplock.Record("other command", 999999, NOW).to_json()
                elif scenario == "no-gpu":
                    host.no_gpu = True
                elif scenario == "site":
                    site_result = site.SiteLoadResult(errors=(site.SiteError(None, REASON, "Repair the site file."),))
                elif scenario == "engine":
                    target = Problem("generator unavailable", "Run engine verify, then retry.")
                elif scenario == "no-generation":
                    host.generations = []
                elif scenario == "generation-name":
                    generation = "not-recorded"
                elif scenario == "generation-exit":
                    host.exit_codes["generations"] = 3
                elif scenario == "generation-unavailable":
                    host.exit_codes["generations"] = 127
                elif scenario == "generation-json":
                    host.responses["generations"] = REASON
                elif scenario == "manifest-exit":
                    host.exit_codes["manifest"] = 3
                elif scenario == "manifest-row":
                    host.responses["manifest"] = json.dumps({"source_id": "bad"})
                elif scenario == "source-id":
                    host.manifest_rows[0]["source_id"] = "fictitious/not-an-opinion"
                elif scenario == "facts-exit":
                    host.exit_codes["facts"] = 3
                elif scenario == "facts-unavailable":
                    host.run_errors["facts"] = FileNotFoundError(REASON)
                elif scenario == "facts-json":
                    host.responses["facts"] = REASON
                elif scenario == "facts-row":
                    host.documents[0]["decided_date"] = "bad-date"
                elif scenario == "case-name-type":
                    host.documents[0]["case_name"] = {"invalid": CASE_NAME}
                code, output, errors = _run(host, root, out, category=category, generation=generation, site_result=site_result, target=target)
                self.assertEqual((code, output), (1, ""))
                self.assertIn(": refused —", errors)
                self.assertIn("Fix:", errors)
                self.assertEqual(host.bodies, [])
                self.assertEqual(host.released, [] if scenario in {"root", "set", "category", "inside", "nonempty", "holder"} else [backuplock.ENGINE_LOCK.path])
                if scenario in {"no-generation", "generation-name", "source-id"}:
                    self.assertIn("index build caselaw --sample", errors)
                if scenario in {"generation-exit", "generation-unavailable", "facts-exit", "facts-unavailable"}:
                    self.assertIn("apply", errors)
                if scenario == "root":
                    self.assertIn("sudo python3 -B -m tools.synthetic.draft", errors)
                self.assertFalse((out / "manifest.json").exists())
                self.assert_private_absent(output + errors)

    def test_corrupt_store_write_failure_and_interrupt_release_lock(self) -> None:
        for scenario in ("missing", "corrupt", "write", "interrupt", "directory"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost(1)
                if scenario == "missing":
                    host.objects.clear()
                elif scenario == "corrupt":
                    host.objects[host.manifest_rows[0]["canonical_text_sha256"]] = b"Corrupt fictitious object"
                elif scenario == "interrupt":
                    host.replies = [KeyboardInterrupt()]
                patched = (
                    patch.object(draft, "write_atomic", side_effect=OSError(REASON)) if scenario == "write" else
                    patch.object(draft, "make_directory", side_effect=OSError(REASON)) if scenario == "directory" else contextlib.nullcontext()
                )
                with patched:
                    if scenario == "interrupt":
                        with self.assertRaises(KeyboardInterrupt):
                            _run(host, root, root / "draft", count=1)
                    else:
                        code, output, errors = _run(host, root, root / "draft", count=1)
                        self.assertEqual(code, 1)
                        self.assertIn("Fix:", errors)
                        self.assert_private_absent(output + errors)
                self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
                self.assertEqual((root / "draft/manifest.json").exists(), scenario in {"missing", "corrupt", "interrupt"})
                if scenario in {"missing", "corrupt", "interrupt"}:
                    self.assert_checkpoint(root / "draft", 0)
                self.assertEqual(list(root.rglob("*.partial")), [])

    def test_window_uses_canonical_head_at_paragraph_boundary(self) -> None:
        text = PASSAGE + "\n\n" + "x" * synthetic.TEMPLATE_WINDOW_CHARS
        self.assertEqual(draft._window(text), PASSAGE)
        self.assertEqual(draft._window(PASSAGE), PASSAGE)
        unbroken = "x" * (synthetic.TEMPLATE_WINDOW_CHARS + 1)
        self.assertEqual(draft._window(unbroken), unbroken[:synthetic.TEMPLATE_WINDOW_CHARS])

    def test_usage_error_does_not_take_lock(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = draft.main(["--category", "lookup", "--count", "0", "--seed", "1", "--out", "unused"], stdout=output)
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("count must be positive", errors.getvalue())


class NearMissDraft(unittest.TestCase):
    """Near misses are checked against the record before generation."""

    def test_draw_slots_sheet_and_format_three_intake_for_each_category(self) -> None:
        for category in synthetic.NEAR_MISS_CATEGORIES:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = SyntheticHost(3)
                out = root / "draft"
                code, output, errors = _run(host, root, out, count=3, category=category)
                self.assertEqual((code, errors), (0, ""), output)
                other = SyntheticHost(3)
                other.documents.reverse()
                other.manifest_rows.reverse()
                repeat = root / "repeat"
                self.assertEqual(_run(other, root, repeat, count=3, category=category), (code, output, errors))
                for path in out.iterdir():
                    self.assertEqual(path.read_bytes(), (repeat / path.name).read_bytes())
                value = json.loads((out / "manifest.json").read_bytes())
                self.assertEqual(value["format"], 3)
                self.assertEqual(tuple(value), manifest.MANIFEST_KEYS)
                records = [json.loads(line) for line in (out / "drafts.jsonl").read_bytes().splitlines()]
                sheet = (out / "sheet.txt").read_text()
                absent = tuple(sorted(court for court, fact in host.court_map.courts.items()
                                      if fact.level == "circuit" and court not in host.present_courts))
                for index, row in enumerate(records):
                    source = row["gold"][0]["source_id"]
                    fact = next(fact for fact in host.documents if source == indexsample.source_id("fictitious", cast(int, fact["opinion_id"])))
                    user = cast(list[dict[str, str]], host.bodies[index * 3]["messages"])[1]["content"]
                    expected = {"answer": ANSWER, "behavior": synthetic.BEHAVIORS[category]}
                    if category == "poisoned-premise":
                        near = synthetic.invent_caption(value["seed"], source)
                        expected["premise"] = near
                        self.assertEqual(row["jurisdiction"], [fact["court"]])
                        self.assertIn(f"Premise: {near}\n", user)
                        near_line = f"Near miss: premise {near} | behavior {synthetic.BEHAVIORS[category]}"
                    else:
                        near = synthetic.asked_court(value["seed"], source, absent)
                        self.assertEqual(row["jurisdiction"], [near])
                        self.assertNotEqual(near, fact["court"])
                        name = host.court_map.courts[near].name
                        self.assertIn(f"Asked court: {name}\n", user)
                        near_line = f"Near miss: asked court {near} ({name}) | behavior {synthetic.BEHAVIORS[category]}"
                    self.assertEqual(row["expected"], expected)
                    self.assertNotIn("must_cite", row["expected"])
                    text = host.texts[cast(int, fact["opinion_id"])]
                    gold = row["gold"][0]
                    self.assertEqual(text[gold["start"]:gold["end"]], gold["quote"])
                    block = sheet.split(f"{manifest.SHEET_BLOCK_HEADER} {index + 1:02d} ")[1]
                    self.assertIn("\n" + near_line + "\nQuestion: ", block)
                    self.assertLess(block.index("Original:"), block.index(near_line))
                stage = "premises" if category == "poisoned-premise" else "courts"
                statements = [sql for _, sql in host.calls if sql and (
                    "jsonb_array_elements(:'premises'::jsonb)" in sql if stage == "premises"
                    else "SELECT DISTINCT court FROM opinions" in sql
                )]
                self.assertEqual(len(statements), 1)
                if stage == "premises":
                    pairs = json.loads(read_psql_set(cast(str, statements[0]).splitlines()[0], "premises"))
                    self.assertEqual(pairs, [list(synthetic.caption_surnames(caption)) for caption in sorted({
                        synthetic.invent_caption(value["seed"], row["gold"][0]["source_id"]) for row in records
                    })])
                    self.assertEqual(cast(str, statements[0]).count("ILIKE"), 2)
                    self.assertIn("pair->>0", cast(str, statements[0]))
                    self.assertIn("pair->>1", cast(str, statements[0]))
                    self.assertIn(f"near-miss: premises {len(pairs)}, in record 0", output)
                else:
                    self.assertIn(f"near-miss: absent courts {len(absent)}", output)
                marks = root / "marks.txt"
                marks.write_text(f"synthetic: CSA-1 {value['sheet_sha256'][:manifest.DIGEST_PREFIX_CHARS]} 2099-01-04\n"
                                 + "".join(f"{index:02d} accept\n" for index in range(1, len(records) + 1)))
                result = _run_intake(out / "manifest.json", marks, tuple(host.court_map.courts))
                self.assertEqual((result[0], result[2]), (0, ""), result[1])
                self.assertTrue(evalset.load_set(root / "eval-v9", host.court_map.courts).ok)

    def test_existing_premises_are_discarded_before_any_engine_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = SyntheticHost(3)
            captions = [synthetic.invent_caption(17, row["source_id"]) for row in host.manifest_rows]
            host.case_names = [f"Fictitious {caption.upper()} appeal" for caption in captions]
            code, output, errors = _run(host, root, root / "draft", category="poisoned-premise")
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual((host.bodies, host.fetched), ([], []))
            value = json.loads((root / "draft/manifest.json").read_bytes())
            self.assertEqual(value["entries"], [])
            self.assertEqual(value["discards"], [
                {"reason": "premise-in-record", "source_id": row["source_id"], "edge": None, "kind": None}
                for row in host.manifest_rows
            ])
            self.assertIn(f"near-miss: premises {len(set(captions))}, in record {len(set(captions))}", output)
            self.assertIn("premise-in-record=3", output)
            self.assertFalse(any("discarded premise-in-record" in line for line in output.splitlines()))

    def test_all_circuits_present_and_map_failure_refuse_before_requests(self) -> None:
        for category in ("unanswerable", "trap"):
            for scenario in ("all-present", "map-failure"):
                with self.subTest(category=category, scenario=scenario), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    host = SyntheticHost(1)
                    host.present_courts = list(host.court_map.courts)
                    code, output, errors = _run(host, root, root / "draft", category=category,
                        court_result=courts.CourtMapLoadResult() if scenario == "map-failure" else None)
                    self.assertEqual(code, 1)
                    self.assertIn("courts.yaml from the release checkout" if scenario == "map-failure"
                                  else "every circuit-level court is in the record", errors)
                    self.assertIn("Fix:", errors)
                    self.assertEqual(host.bodies, [])
                    self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
                    for private in (QUESTION, ANSWER, CLOSED, REASON, CASE_NAME):
                        self.assertNotIn(private, output + errors)

    def test_record_failures_and_malformed_replies_refuse_with_fixes(self) -> None:
        for category, stage in (("trap", "courts"), ("poisoned-premise", "premises")):
            malformed = ("not json", "{}", '[null]', '[{}]', '["not a court"]', '["exampleca", "exampleca"]') if stage == "courts" else (
                "not json", "{}", '[null]', '[[]]', '[["Fictitious", "Unknown"]]',
            )
            for scenario in ("exit", "unavailable", *malformed):
                with self.subTest(stage=stage, scenario=scenario), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    host = SyntheticHost(1)
                    if scenario == "exit":
                        host.exit_codes[stage] = 9
                    elif scenario == "unavailable":
                        host.run_errors[stage] = OSError(REASON)
                    else:
                        host.responses[stage] = scenario
                    code, output, errors = _run(host, root, root / "draft", category=category)
                    self.assertEqual(code, 1)
                    self.assertIn(f"{stage}: refused", errors)
                    self.assertIn("Fix:", errors)
                    self.assertEqual(host.bodies, [])
                    self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
                    self.assertNotIn(REASON, output + errors)


class TreatmentDraft(unittest.TestCase):
    """Pattern edges fix their evidence through the worker's real sentence rule."""

    def assert_private_absent(self, value: str) -> None:
        for private in (QUESTION, ANSWER, CLOSED, REASON, TREATMENT_SENTENCE, "Fable v. Example"):
            self.assertNotIn(private, value)

    def test_duplicates_are_skipped_at_submission_and_ordered_consumption(self) -> None:
        for window in (1, 3):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = TreatmentHost(3)
                first, second, third = host.edge_rows
                second.update(state="negative", cluster_state="negative", cluster=first["cluster"])
                host.citation_rows[cast(str, second["doc_id"])][0]["to_cluster"] = first["cluster"]
                duplicate = {**first, "citation_id": "a" * 64, "signal_id": "b" * 64}
                citations = host.citation_rows[cast(str, first["doc_id"])]
                citations.append({**citations[0], "citation_id": duplicate["citation_id"], "ordinal": 1})
                host.edge_rows = [first, second, duplicate, third]
                submitted: list[str] = []
                finished: list[str] = []
                later_done = [threading.Event(), threading.Event()]
                original: Callable[..., object] = draft._document

                def work(*args: object, original: Callable[..., object] = original, window: int = window,
                         first: dict[str, object] = first, second: dict[str, object] = second,
                         duplicate: dict[str, object] = duplicate, later_done: list[threading.Event] = later_done,
                         finished: list[str] = finished) -> object:
                    document = cast(synthetic.DrawDocument, args[4])
                    value = original(*args)
                    if window > 1 and document.item == first["citation_id"]:
                        for done in later_done:
                            self.assertTrue(done.wait(5), "later duplicate did not finish")
                    finished.append(document.item)
                    if document.item == second["citation_id"]:
                        later_done[0].set()
                    elif document.item == duplicate["citation_id"]:
                        later_done[1].set()
                    return value

                submit = ThreadPoolExecutor.submit

                def record_submit(executor: ThreadPoolExecutor, function: Callable[..., object], *args: object,
                                  submitted: list[str] = submitted, submit: Callable[..., object] = submit,
                                  **kwargs: object) -> object:
                    submitted.append(cast(synthetic.DrawDocument, args[4]).item)
                    return submit(executor, function, *args, **kwargs)

                with (
                    patch.object(synthetic, "draw_order", side_effect=lambda documents, _seed: tuple(documents)),
                    patch.object(draft, "_document", side_effect=work),
                    patch.object(ThreadPoolExecutor, "submit", new=record_submit),
                ):
                    code, output, errors = _run(host, root, root / "draft", category="treatment", count=3, window=window)
                self.assertEqual((code, errors), (0, ""), output)
                provenance = json.loads((root / "draft/manifest.json").read_bytes())
                self.assertEqual([entry["source_id"] for entry in provenance["entries"]],
                                 [host.manifest_rows[index]["source_id"] for index in (0, 2)])
                self.assertEqual(provenance["discards"], [
                    {"reason": "same-cluster", "source_id": host.manifest_rows[1]["source_id"],
                     "edge": second["citation_id"], "kind": "negative"},
                    {"reason": "same-document", "source_id": host.manifest_rows[0]["source_id"],
                     "edge": duplicate["citation_id"], "kind": "negative"},
                ])
                expected = host.edge_rows if window > 1 else [first, third]
                self.assertEqual(submitted, [row["citation_id"] for row in expected])
                self.assertEqual(len(host.bodies), 3 * len(expected))
                if window > 1:
                    self.assertGreater(finished.index(cast(str, first["citation_id"])),
                                       finished.index(cast(str, second["citation_id"])))
                    self.assertGreater(finished.index(cast(str, first["citation_id"])),
                                       finished.index(cast(str, duplicate["citation_id"])))
                self.assertIn("order: exhausted, kept 2 of 3", output)
                self.assert_private_absent(output + errors)

    def test_unfinished_answer_in_each_new_category_keeps_zero_without_reading(self) -> None:
        for category in ("treatment", "section-attribution"):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = TreatmentHost(1) if category == "treatment" else AttributionHost()
                proposal = _reply({"question": QUESTION, "answer": ANSWER}) if category == "treatment" else _proposal(MAJORITY_PASSAGE)
                host.replies = [proposal, _reply({}, finish="length")]
                with patch.object(synthetic, "read_closed_book", wraps=synthetic.read_closed_book) as read:
                    code, output, errors = _run(host, root, root / "draft", category=category, count=1)
                self.assertEqual((code, errors), (0, ""), output)
                read.assert_not_called()
                provenance = json.loads((root / "draft/manifest.json").read_bytes())
                self.assertEqual(provenance["entries"][0]["closed_book_score"], 0)
                self.assertIsNone(provenance["entries"][0]["label"])
                case = json.loads((root / "draft/drafts.jsonl").read_bytes())
                self.assertEqual(case["closed_book_answer"], synthetic.CLOSED_BOOK_UNFINISHED)
                self.assertEqual(case["labels"], [synthetic.SYNTHETIC_ORIGIN])
                self.assertEqual(len(host.bodies), 2)

    def test_three_kinds_gold_slots_sentence_sheet_and_intake(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = TreatmentHost()
            with patch.object(treatment, "sentence", wraps=treatment.sentence) as sentence:
                code, output, errors = _run(host, root, root / "draft", category="treatment", count=3)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(sentence.call_count, 3)
            for call in sentence.call_args_list:
                self.assertIsInstance(call.args[3], draft.SectionRow)
                self.assertTrue(all(type(citation) is NewCitation for citation in call.args[4]))
                self.assertIs(call.args[5], treatment.RULES)
            provenance = json.loads((root / "draft/manifest.json").read_bytes())
            self.assertEqual(provenance["discards"], [])
            lines = (root / "draft/drafts.jsonl").read_bytes().splitlines()
            cases = [json.loads(line) for line in lines]
            self.assertEqual({case["expected"]["state"] for case in cases}, set(synthetic.TREATMENT_KINDS))
            by_id = {row["citation_id"]: row for row in host.edge_rows}
            for ordinal, case in enumerate(cases, start=1):
                gold = case["gold"][0]
                edge = by_id[gold["citation_id"]]
                self.assertEqual(tuple(gold), evalset.SHAPE_REGISTRY[("research-qa", "treatment")].gold_keys_for())
                self.assertEqual((gold["signal_id"], gold["pattern_set"], gold["cluster"]),
                                 (edge["signal_id"], PATTERN_SET_ID, edge["cluster"]))
                self.assertEqual(gold["quote"], TREATMENT_SENTENCE)
                self.assertEqual(host.texts[cast(int, edge["opinion_id"])][gold["start"]:gold["end"]], gold["quote"])
                self.assertEqual(case["expected"]["must_cite"], [gold["source_id"]])
                self.assertEqual(case["cluster_id"], gold["source_id"])
                self.assertIn(f"{ordinal:02d}: {gold['source_id']} {gold['citation_id']} {case['expected']['state']} [{gold['start']}, {gold['end']})", output)
                complete = {key: f"research-qa-treatment-{ordinal:03d}" if key == "id" else
                            {"by": "CSA-1", "on": "2099-01-04"} if key == "review" else case[key]
                            for key in synthetic.CASE_KEYS}
                self.assertEqual(evalset.case_findings(complete, synthetic.case_path("treatment"), ordinal), ())
            proposals = [body for body in host.bodies if cast(dict[str, dict[str, object]], body["response_format"])["json_schema"]["name"] == "synthetic_treatment"]
            self.assertEqual(len(proposals), 3)
            for body in proposals:
                user = cast(list[dict[str, str]], body["messages"])[1]["content"]
                self.assertIn(TREATMENT_SENTENCE, user)
                self.assertIn("Fable v. Example", user)
                self.assertNotIn("Fictitious later paragraph", user)
            sheet = (root / "draft/sheet.txt").read_bytes()
            self.assertEqual(hashlib.sha256(sheet).hexdigest(), provenance["sheet_sha256"])
            self.assertIn(b"Cited parties: Fable v. Example", sheet)
            self.assertIn(b"Cited original: https://www.courtlistener.com/c/F.3d/123/456/", sheet)
            self.assertIn(b"standing dissent", sheet)
            self.assertIn(b"Edge: ", sheet)
            self.assertIn("kinds negative=1, caution=1, none=1", output)
            sql = next(sql for _, sql in host.calls if sql is not None and "AS treatment_edges" in sql)
            risk = sql.split("FROM citation_signals risk", 1)[1].split(") AS cluster_state", 1)[0]
            self.assertIn("resolved.to_cluster = c.to_cluster", risk)
            self.assertIn("JOIN citations resolved ON resolved.citation_id = risk.citation_id "
                          "AND resolved.pipeline = :'pipeline'", risk)
            self.assertIn("JOIN citations c ON c.citation_id = s.citation_id "
                          "AND c.pipeline = :'pipeline'", sql)
            for marker, condition in (
                ("AS treatment_edges", "c.pipeline = :'pipeline'"),
                ("AS section_rows", "doc_id = :'doc_id' AND pipeline = :'pipeline'"),
                ("AS citation_rows", "doc_id = :'doc_id' AND pipeline = :'pipeline'"),
            ):
                reads = [(argv, statement) for argv, statement in host.calls
                         if statement is not None and marker in statement]
                self.assertTrue(reads)
                for argv, statement in reads:
                    assert statement is not None
                    self.assertEqual(statement.count("\\set pipeline "), 1)
                    self.assertIn(worker.bind("pipeline", PIPELINE_VERSION), statement)
                    self.assertNotIn(PIPELINE_VERSION, " ".join(argv))
                    self.assertIn(condition, statement)
            self.assertNotIn("opinion_ids", risk)
            self.assertIn("ready.status = 'ready'", sql)
            self.assertIn("ORDER BY cited.decided_date, cited.opinion_id LIMIT 1", sql)
            self.assertIn("known.overruled_cluster = c.to_cluster", sql)
            self.assert_private_absent(output + errors + (root / "draft/manifest.json").read_text())
            marks = root / "marks.txt"
            marks.write_text(f"synthetic: CSA-1 {provenance['sheet_sha256']} 2099-01-04\n01 accept\n02 accept\n03 accept\n")
            code, output, errors = _run_intake(root / "draft/manifest.json", marks, tuple(str(fact["court"]) for fact in host.documents))
            self.assertEqual((code, errors), (0, ""), output)
            self.assertTrue(evalset.load_set(root / "eval-v9").ok)
            self.assert_private_absent(output)

    def test_draw_skips_each_edge_for_absence_name_and_risk_precedence(self) -> None:
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"cited": None}, "cited-absent"),
            ({"state": "caution", "cluster_state": "negative"}, "state-outranked"),
            ({"state": "caution", "cluster_state": "caution", "listed": True}, "state-outranked"),
            ({"state": None, "no_state_reason": "non_holding", "cluster_state": "negative"}, "state-outranked"),
            ({"state": None, "no_state_reason": "non_holding", "cluster_state": "caution"}, "state-outranked"),
            ({"state": None, "no_state_reason": "non_holding", "cluster_state": None, "listed": True}, "state-outranked"),
            ({"state": None, "no_state_reason": "lineage_unverified", "signal": "reversed"}, "no-state-kind"),
            ({"state": None, "no_state_reason": "lineage_unverified", "signal": "vacated"}, "no-state-kind"),
            ({"state": None, "no_state_reason": "unresolved"}, "no-state-kind"),
            ({"state": None, "no_state_reason": "non_holding", "cluster_state": None, "standing": "unknown"}, "no-state-kind"),
            ({"state": None, "no_state_reason": "non_holding", "cluster_state": None, "standing": "footnote"}, "no-state-kind"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = TreatmentHost(1)
            base = host.edge_rows[0]
            host.edge_rows = [
                {**base, **changes, "citation_id": hashlib.sha256(f"fictitious skipped edge {index}".encode()).hexdigest()}
                for index, (changes, _) in enumerate(cases)
            ]
            for index, name in enumerate((None, "", " \t ")):
                host.edge_rows.append({**base, "citation_id": hashlib.sha256(f"fictitious unnamed {index}".encode()).hexdigest(),
                                       "cited": {**cast(dict[str, object], base["cited"]), "case_name": name}})
            code, output, errors = _run(host, root, root / "draft", category="treatment", count=1)
            self.assertEqual((code, errors), (0, ""), output)
            provenance = json.loads((root / "draft/manifest.json").read_bytes())
            self.assertEqual([row["reason"] for row in provenance["discards"]], [reason for _, reason in cases] + ["no-case-name"] * 3)
            self.assertEqual([row["edge"] for row in provenance["discards"]], [row["citation_id"] for row in host.edge_rows])
            self.assertEqual((host.fetched, host.bodies), ([], []))
            self.assertIn(f"treatment: {len(host.edge_rows)} edges", output)
            self.assert_private_absent(output + errors)

    def test_duplicate_document_and_cluster_skip_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = TreatmentHost(2)
            first, second = host.edge_rows
            second.update(state="negative", cluster_state="negative", cluster=first["cluster"])
            duplicate = {**first, "citation_id": "a" * 64, "signal_id": "b" * 64}
            host.edge_rows.append(duplicate)
            with patch.object(synthetic, "draw_order", side_effect=lambda documents, _seed: tuple(documents)):
                code, output, errors = _run(host, root, root / "draft", category="treatment", count=3)
            self.assertEqual((code, errors), (0, ""), output)
            provenance = json.loads((root / "draft/manifest.json").read_bytes())
            self.assertEqual(len(provenance["entries"]), 1)
            self.assertEqual([row["reason"] for row in provenance["discards"]], ["same-cluster", "same-document"])
            self.assertEqual(len(host.fetched), 1)
            self.assertEqual(len(host.bodies), 3)
            marks = root / "marks.txt"
            marks.write_text(f"synthetic: CSA-1 {provenance['sheet_sha256']} 2099-01-04\n01 accept\n")
            code, output, errors = _run_intake(root / "draft/manifest.json", marks, tuple(str(fact["court"]) for fact in host.documents))
            self.assertEqual((code, errors), (0, ""), output)

    def test_verb_outside_and_forbidden_answer_discard_before_closed_book(self) -> None:
        for reason in ("verb-outside", "good-law-wording"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = TreatmentHost(1)
                if reason == "verb-outside":
                    host.edge_rows[0].update(verb_start=0, verb_end=1)
                else:
                    host.replies = [_reply({"question": QUESTION, "answer": "The fictitious case is GOOD LAW."})]
                code, output, errors = _run(host, root, root / "draft", category="treatment", count=1)
                self.assertEqual((code, errors), (0, ""), output)
                provenance = json.loads((root / "draft/manifest.json").read_bytes())
                self.assertEqual(provenance["discards"][0]["reason"], reason)
                self.assertEqual(len(host.bodies), 0 if reason == "verb-outside" else 1)
                self.assert_private_absent(output + errors)

    def test_record_read_failures_and_disagreements_refuse_with_apply_logs_fix(self) -> None:
        for stage in ("edges", "sections", "citations"):
            for scenario in ("exit", "unavailable", "json", "shape", "missing"):
                with self.subTest(stage=stage, scenario=scenario), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    host = TreatmentHost(1)
                    if scenario == "exit":
                        host.exit_codes[stage] = 7
                    elif scenario == "unavailable":
                        host.run_errors[stage] = FileNotFoundError(REASON)
                    elif scenario == "json":
                        host.responses[stage] = REASON
                    elif scenario == "shape":
                        host.responses[stage] = json.dumps([{"extra": QUESTION}])
                    elif stage == "edges":
                        del host.edge_rows[0]["qualifier"]
                    else:
                        host.responses[stage] = "[]"
                    code, output, errors = _run(host, root, root / "draft", category="treatment", count=1)
                    self.assertEqual(code, 1)
                    self.assertIn("Fix: Run", errors)
                    self.assertIn("apply", errors)
                    self.assertIn("logs", errors)
                    self.assertNotIn("internal error", errors)
                    self.assertEqual(host.bodies, [])
                    self.assertEqual((root / "draft/manifest.json").exists(), stage in {"sections", "citations"})
                    self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
                    self.assert_private_absent(output + errors)

    def test_malformed_edge_fields_and_cited_facts_are_content_free_refusals(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("opinion_id", True), ("doc_id", "bad"), ("court", QUESTION),
            ("decided_date", "bad"), ("reporter_cites", []), ("case_name", QUESTION),
            ("citation_id", "bad"), ("signal_id", "A" * 64), ("pattern_set", "fictitious/patterns@1"),
            ("signal", QUESTION), ("qualifier", []), ("standing", "holding"), ("section_type", None),
            ("state", "none"), ("state", None), ("no_state_reason", "non_holding"),
            ("verb_start", True), ("verb_end", 0), ("citation_start", -1), ("citation_end", False),
            ("section_id", "bad"), ("cluster", True), ("cluster_state", "good law"), ("listed", 1),
            ("cited", []), ("cited", {"extra": QUESTION}),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = TreatmentHost(1)
                host.edge_rows[0][key] = value
                host.responses["edges"] = json.dumps(host.edge_rows)
                code, output, errors = _run(host, root, root / "draft", category="treatment", count=1)
                self.assertEqual(code, 1)
                self.assertIn("treatment: refused", errors)
                self.assertIn("apply", errors)
                self.assertIn("logs", errors)
                self.assertNotIn("internal error", errors)
                self.assertEqual((host.fetched, host.bodies), ([], []))
                self.assert_private_absent(output + errors)
        for key, value in (("opinion_id", False), ("court", "bad court"), ("decided_date", "bad"),
                           ("reporter_cites", [None]), ("case_name", 1)):
            with self.subTest(cited=key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = TreatmentHost(1)
                cast(dict[str, object], host.edge_rows[0]["cited"])[key] = value
                code, output, errors = _run(host, root, root / "draft", category="treatment", count=1)
                self.assertEqual(code, 1)
                self.assertIn("invalid cited facts", errors)
                self.assert_private_absent(output + errors)

    def test_document_row_validation_uses_real_citation_shape_and_record_agreement(self) -> None:
        cases: tuple[tuple[str, str, object], ...] = (
            ("sections", "section_id", "bad"), ("sections", "section_type", "holding"),
            ("sections", "char_start", True), ("sections", "char_end", 0),
            ("citations", "doc_id", "bad"), ("citations", "citation_id", "bad"),
            ("citations", "ordinal", True), ("citations", "char_start", -1),
            ("citations", "char_end", False), ("citations", "section_id", "bad"),
            ("citations", "section_type", None), ("citations", "cite_type", "bad"),
            ("citations", "cite_form", "bad"), ("citations", "raw_cite", QUESTION),
            ("citations", "reporter_cite", 1), ("citations", "pincite", []),
            ("citations", "key", {}), ("citations", "to_cluster", False), ("citations", "pattern_id", ""),
            ("citations", "citation_id", "a" * 64), ("citations", "to_cluster", 9999),
            ("sections", "section_type", "dissent"),
        )
        for stage, key, value in cases:
            with self.subTest(stage=stage, key=key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = TreatmentHost(1)
                rows = host.section_rows if stage == "sections" else host.citation_rows
                rows[cast(str, host.documents[0]["doc_id"])][0][key] = value
                code, output, errors = _run(host, root, root / "draft", category="treatment", count=1)
                self.assertEqual(code, 1)
                self.assertIn("apply", errors)
                self.assertIn("logs", errors)
                self.assertNotIn("internal error", errors)
                self.assertEqual(host.bodies, [])
                self.assert_private_absent(output + errors)

    def test_treatment_draw_and_walk_apply_shared_floors_before_requests(self) -> None:
        for scenario in ("unpublished", "no-case-name", "document-short"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = TreatmentHost(1)
                if scenario == "unpublished":
                    host.documents[0]["precedential"] = "unpublished"
                elif scenario == "no-case-name":
                    host.documents[0]["case_name"] = None
                else:
                    text = host.texts[101][:synthetic.DOCUMENT_MIN_CHARS - 1]
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    host.objects[digest] = text.encode()
                    host.documents[0]["canonical_sha256"] = digest
                    host.manifest_rows[0]["canonical_text_sha256"] = digest
                code, output, errors = _run(host, root, root / "draft", category="treatment", count=1)
                self.assertEqual((code, errors), (0, ""), output)
                provenance = json.loads((root / "draft/manifest.json").read_bytes())
                self.assertEqual(provenance["discards"][0]["reason"], scenario)
                self.assertEqual(len(host.fetched), int(scenario == "document-short"))
                self.assertEqual(host.bodies, [])
                self.assertFalse(any("AS section_rows" in (sql or "") for _, sql in host.calls))
                if scenario == "document-short":
                    self.assertEqual(provenance["discards"][0]["edge"], host.edge_rows[0]["citation_id"])
                    self.assertEqual(provenance["discards"][0]["kind"], "negative")
                else:
                    sql = next(sql for _, sql in host.calls if sql and "AS treatment_edges" in sql)
                    self.assertEqual(json.loads(read_psql_set(sql.splitlines()[0], "opinion_ids")), [])


class AttributionDraft(unittest.TestCase):
    """Attribution cases select and retrace evidence within one recorded section."""

    def assert_private_absent(self, text: str) -> None:
        for private in (QUESTION, ANSWER, CLOSED, REASON, MAJORITY_PASSAGE, DISSENT_PASSAGE, SYLLABUS_PASSAGE):
            self.assertNotIn(private, text)

    def test_window_skips_a_kept_document_before_submit_and_after_work_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = AttributionHost(2)
            submitted: list[tuple[int, str]] = []
            later_done = threading.Event()
            original: Callable[..., object] = draft._document
            submit = ThreadPoolExecutor.submit

            def work(*args: object) -> object:
                document = cast(synthetic.DrawDocument, args[4])
                value = original(*args)
                if document.opinion_id == 101:
                    self.assertTrue(later_done.wait(5), "second opinion did not finish")
                else:
                    later_done.set()
                return value

            def record_submit(executor: ThreadPoolExecutor, function: Callable[..., object], *args: object, **kwargs: object) -> object:
                document = cast(synthetic.DrawDocument, args[4])
                submitted.append((document.opinion_id, document.kind))
                return submit(executor, function, *args, **kwargs)

            with (
                patch.object(draft, "_document", side_effect=work),
                patch.object(ThreadPoolExecutor, "submit", new=record_submit),
            ):
                code, output, errors = _run(host, root, root / "draft", category="section-attribution", count=3, window=2)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(submitted, [(101, "holding"), (102, "holding"), (102, "separate")])
            provenance = json.loads((root / "draft/manifest.json").read_bytes())
            self.assertEqual([entry["source_id"] for entry in provenance["entries"]],
                             [row["source_id"] for row in host.manifest_rows])
            self.assertEqual(provenance["discards"], [
                {"reason": "same-document", "source_id": row["source_id"], "edge": None, "kind": "separate"}
                for row in host.manifest_rows
            ])
            self.assertEqual(len(host.bodies), 9)
            self.assertIn("order: exhausted, kept 2 of 3", output)
            self.assert_private_absent(output + errors)

    def test_failed_holding_in_window_still_allows_separate_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = AttributionHost()
            original = host.engine_call

            def reply(*args: object, **kwargs: object) -> engine.EngineReply:
                body = cast(dict[str, object], kwargs["body"])
                messages = cast(list[dict[str, str]], body["messages"])
                if "Kind: holding\n" in messages[1]["content"]:
                    host.bodies.append(body)
                    return _proposal(SYLLABUS_PASSAGE)
                return original(*args, **kwargs)

            with patch.object(host, "engine_call", side_effect=reply):
                code, output, errors = _run(host, root, root / "draft", category="section-attribution", count=1, window=2)
            self.assertEqual((code, errors), (0, ""), output)
            case = json.loads((root / "draft/drafts.jsonl").read_bytes())
            self.assertEqual(case["expected"]["section_type"], "dissent")
            provenance = json.loads((root / "draft/manifest.json").read_bytes())
            self.assertEqual(provenance["discards"], [
                {"reason": "off-section", "source_id": host.manifest_rows[0]["source_id"], "edge": None, "kind": "holding"},
            ])
            self.assert_private_absent(output + errors)

    def test_each_kind_keeps_one_document_with_sections_only_and_intakes(self) -> None:
        draw = synthetic.draw_order
        for kind, section_type, passage in (("holding", "majority", MAJORITY_PASSAGE), ("separate", "dissent", DISSENT_PASSAGE)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = AttributionHost()
                with patch.object(synthetic, "draw_order", side_effect=lambda documents, seed, kind=kind: sorted(
                    draw(documents, seed), key=lambda document: document.kind != kind,
                )):
                    code, output, errors = _run(host, root, root / "draft", category="section-attribution", count=2)
                self.assertEqual((code, errors), (0, ""), output)
                provenance = json.loads((root / "draft/manifest.json").read_bytes())
                case = json.loads((root / "draft/drafts.jsonl").read_bytes())
                gold = case["gold"][0]
                self.assertEqual(tuple(case["expected"]), evalset.SHAPE_REGISTRY[("research-qa", "section-attribution")].expected_keys_for())
                self.assertEqual(case["expected"]["section_type"], section_type)
                self.assertEqual(tuple(gold), evalset.GOLD_COORDINATE_KEYS)
                self.assertEqual(gold["quote"], passage)
                self.assertEqual(host.texts[101][gold["start"]:gold["end"]], passage)
                self.assertEqual(case["cluster_id"], gold["source_id"])
                self.assertEqual(case["expected"]["must_cite"], [gold["source_id"]])
                self.assertIn(f"01: {gold['source_id']} {kind} {section_type} [{gold['start']}, {gold['end']})", output)
                other = next(value for value in synthetic.ATTRIBUTION_KINDS if value != kind)
                self.assertEqual(provenance["discards"], [{"reason": "same-document", "source_id": gold["source_id"], "edge": None, "kind": other}])
                self.assertIn(f"{gold['source_id']}: discarded same-document\n", output)
                self.assertIn("sections: eligible holding=1, separate=1, no-kind 0", output)
                self.assertIn(f"kinds holding={int(kind == 'holding')}, separate={int(kind == 'separate')}", output)
                self.assertEqual(len(host.fetched), 1)
                self.assertEqual(len(host.bodies), 3)
                reads = [sql or "" for _, sql in host.calls]
                self.assertEqual(sum("AS section_summaries" in sql for sql in reads), 1)
                self.assertEqual(sum("AS section_rows" in sql for sql in reads), 1)
                self.assertFalse(any("AS citation_rows" in sql for sql in reads))
                self.assertIn("s.section_type <> 'footnote'", next(sql for sql in reads if "AS section_summaries" in sql))
                for marker, condition in (
                    ("AS section_summaries", "s.pipeline = :'pipeline'"),
                    ("AS section_rows", "doc_id = :'doc_id' AND pipeline = :'pipeline'"),
                ):
                    sql = next(sql for sql in reads if marker in sql)
                    self.assertEqual(sql.count("\\set pipeline "), 1)
                    self.assertIn(worker.bind("pipeline", PIPELINE_VERSION), sql)
                    self.assertIn(condition, sql)
                doc_id = cast(str, host.documents[0]["doc_id"])
                target = next(section for section in host.section_rows[doc_id] if section["section_type"] == section_type)
                start, end = cast(int, target["char_start"]), cast(int, target["char_end"])
                body = host.bodies[0]
                user = cast(list[dict[str, str]], body["messages"])[1]["content"]
                self.assertEqual(user, synthetic.TEMPLATES["section-attribution@1"].user_template.format(
                    court=host.documents[0]["court"], decided=host.documents[0]["decided_date"],
                    cites="; ".join(cast(list[str], host.documents[0]["reporter_cites"])),
                    parties=host.documents[0]["case_name"], kind=kind, section_type=section_type,
                    text=host.texts[101][start:end],
                ))
                complete = {key: "research-qa-section-attribution-001" if key == "id" else
                            {"by": "CSA-1", "on": "2099-01-04"} if key == "review" else case[key]
                            for key in synthetic.CASE_KEYS}
                self.assertEqual(evalset.case_findings(complete, synthetic.case_path("section-attribution"), 1), ())
                sheet = (root / "draft/sheet.txt").read_bytes()
                self.assertEqual(provenance["sheet_sha256"], hashlib.sha256(sheet).hexdigest())
                self.assertIn(f"Kind: {kind} | target section {section_type}".encode(), sheet)
                self.assertIn(("Other section types: " + ("dissent, syllabus" if kind == "holding" else "majority, syllabus")).encode(), sheet)
                self.assertNotIn(b"footnote", sheet)
                self.assert_private_absent(output + errors + (root / "draft/manifest.json").read_text())
                marks = root / "marks.txt"
                marks.write_text(f"synthetic: CSA-1 {provenance['sheet_sha256']} 2099-01-04\n01 accept\n")
                code, output, errors = _run_intake(root / "draft/manifest.json", marks, ("court000",))
                self.assertEqual((code, errors), (0, ""), output)
                self.assertTrue(evalset.load_set(root / "eval-v9").ok)
                self.assert_private_absent(output + errors)

    def test_longest_target_ties_choose_earliest_and_window_is_section_head(self) -> None:
        draw = synthetic.draw_order
        for kind, first_type, second_type, companion in (
            ("holding", "majority", "per_curiam", "dissent"),
            ("separate", "dissent", "concurrence", "majority"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                early = "The fictitious earlier long target describes the invented notice rule. " + MAJORITY_PASSAGE
                late = "The fictitious later tied target discusses another imaginary issue. " + DISSENT_PASSAGE
                length = synthetic.TEMPLATE_WINDOW_CHARS + 500
                host = AttributionHost(blocks=(
                    ("syllabus", SYLLABUS_PASSAGE, 200),
                    (first_type, MAJORITY_PASSAGE, synthetic.SECTION_MIN_CHARS),
                    (first_type, early, length), (second_type, late, length),
                    (companion, DISSENT_PASSAGE, synthetic.SECTION_MIN_CHARS),
                ))
                host.replies = [_proposal(early)]
                with patch.object(synthetic, "draw_order", side_effect=lambda documents, seed, kind=kind: sorted(
                    draw(documents, seed), key=lambda document: document.kind != kind,
                )):
                    code, output, errors = _run(host, root, root / "draft", category="section-attribution", count=1)
                self.assertEqual((code, errors), (0, ""), output)
                case = json.loads((root / "draft/drafts.jsonl").read_bytes())
                self.assertEqual(case["expected"]["section_type"], first_type)
                self.assertEqual(case["gold"][0]["quote"], early)
                user = cast(list[dict[str, str]], host.bodies[0]["messages"])[1]["content"]
                self.assertEqual(user.split("<section-text>\n", 1)[1].split("\n</section-text>", 1)[0], early)
                self.assert_private_absent(output + errors)

    def test_no_kind_counts_document_without_fetch_or_engine_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = AttributionHost(blocks=(("majority", MAJORITY_PASSAGE, synthetic.DOCUMENT_MIN_CHARS),))
            code, output, errors = _run(host, root, root / "draft", category="section-attribution", count=1)
            self.assertEqual((code, errors), (0, ""), output)
            provenance = json.loads((root / "draft/manifest.json").read_bytes())
            self.assertEqual(provenance["discards"], [{"reason": "no-kind", "source_id": host.manifest_rows[0]["source_id"], "edge": None, "kind": None}])
            self.assertIn("sections: eligible holding=0, separate=0, no-kind 1", output)
            self.assertEqual((host.fetched, host.bodies), ([], []))
            self.assert_private_absent(output + errors)

    def test_off_section_under_both_kinds_leaves_distinct_discards_intake_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = AttributionHost()
            host.replies = [_proposal(SYLLABUS_PASSAGE), _proposal(SYLLABUS_PASSAGE)]
            code, output, errors = _run(host, root, root / "draft", category="section-attribution", count=1)
            self.assertEqual((code, errors), (0, ""), output)
            provenance = json.loads((root / "draft/manifest.json").read_bytes())
            self.assertEqual(provenance["entries"], [])
            self.assertEqual(provenance["discards"], [
                {"reason": "off-section", "source_id": host.manifest_rows[0]["source_id"], "edge": None, "kind": kind}
                for kind in synthetic.ATTRIBUTION_KINDS
            ])
            self.assertEqual(len(host.bodies), 2)
            self.assertEqual((root / "draft/drafts.jsonl").read_bytes(), b"")
            self.assert_private_absent(output + errors)
            marks = root / "marks.txt"
            marks.write_text(f"synthetic: CSA-1 {provenance['sheet_sha256']} 2099-01-04\n")
            code, output, errors = _run_intake(root / "draft/manifest.json", marks, ("court000",))
            self.assertEqual((code, errors), (0, ""), output)
            self.assertIn("summary: accepted 0, admitted unread 0, rejected 0", output)
            self.assert_private_absent(output + errors)

    def test_summary_and_section_read_refusals_are_content_free_with_record_fix(self) -> None:
        lengths: tuple[object, ...] = (0, -1, True, "1500", None, {})
        malformed: tuple[object, ...] = (
            None, {}, [], [{"extra": QUESTION}],
            [{"opinion_id": True, "sections": []}], [{"opinion_id": 999, "sections": []}],
            [{"opinion_id": 101, "sections": None}], [{"opinion_id": 101, "sections": ["majority"]}],
            [{"opinion_id": 101, "sections": [["majority"]]}],
            [{"opinion_id": 101, "sections": [["holding", 1000]]}],
            [{"opinion_id": 101, "sections": [["footnote", 1000]]}],
            [{"opinion_id": 101, "sections": []}, {"opinion_id": 101, "sections": []}],
        ) + tuple([{"opinion_id": 101, "sections": [["majority", length]]}] for length in lengths)
        for index, value in enumerate(malformed):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = AttributionHost()
                host.responses["summary"] = json.dumps(value)
                code, output, errors = _run(host, root, root / "draft", category="section-attribution", count=1)
                self.assertEqual(code, 1)
                self.assertIn("sections: refused", errors)
                self.assertIn("apply", errors)
                self.assertIn("logs", errors)
                self.assertNotIn("internal error", errors)
                self.assertEqual((host.fetched, host.bodies), ([], []))
                self.assert_private_absent(output + errors)
        for stage in ("summary", "sections"):
            for scenario in ("exit", "unavailable", "json", "missing"):
                with self.subTest(stage=stage, scenario=scenario), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    host = AttributionHost()
                    if scenario == "exit":
                        host.exit_codes[stage] = 7
                    elif scenario == "unavailable":
                        host.run_errors[stage] = FileNotFoundError(REASON)
                    else:
                        host.responses[stage] = REASON if scenario == "json" else "[]"
                    code, output, errors = _run(host, root, root / "draft", category="section-attribution", count=1)
                    self.assertEqual(code, 1)
                    self.assertIn("apply", errors)
                    self.assertIn("logs", errors)
                    self.assertNotIn("internal error", errors)
                    self.assertEqual(host.bodies, [])
                    self.assertEqual((root / "draft/manifest.json").exists(), stage in {"sections", "citations"})
                    self.assert_private_absent(output + errors)

    def test_attribution_applies_shared_publication_name_and_text_floors(self) -> None:
        for reason in ("unpublished", "no-case-name", "document-short"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = AttributionHost()
                if reason == "unpublished":
                    host.documents[0]["precedential"] = "unpublished"
                elif reason == "no-case-name":
                    host.documents[0]["case_name"] = None
                else:
                    text = host.texts[101][:synthetic.DOCUMENT_MIN_CHARS - 1]
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    host.objects[digest] = text.encode()
                    host.documents[0]["canonical_sha256"] = digest
                    host.manifest_rows[0]["canonical_text_sha256"] = digest
                code, output, errors = _run(host, root, root / "draft", category="section-attribution", count=1)
                self.assertEqual((code, errors), (0, ""), output)
                provenance = json.loads((root / "draft/manifest.json").read_bytes())
                self.assertTrue(all(row["reason"] == reason for row in provenance["discards"]))
                self.assertEqual(len(provenance["discards"]), 2 if reason == "document-short" else 1)
                self.assertEqual(host.bodies, [])
                self.assertFalse(any("AS section_rows" in (sql or "") for _, sql in host.calls))
                if reason != "document-short":
                    self.assertEqual(host.fetched, [])
                self.assert_private_absent(output + errors)


def _held_out_pin(set_root: Path, category: str) -> str:
    path = evalset.held_out_path(set_root / synthetic.case_path(category))
    data = path.read_bytes()
    return f"held-out list: {path.relative_to(set_root).as_posix()} {len(data.splitlines())} lines sha256 {hashlib.sha256(data).hexdigest()}"


def _family_case(value: Mapping[str, object], category: str, number: int) -> dict[str, object]:
    case = _synthetic_case(f"research-qa-{category}-{number:03d}", category=category)
    case.update({key: value[key] for key in ("cluster_id", "gold", "jurisdiction")})
    case["expected"] = {"answer": ANSWER, "must_cite": [value["cluster_id"]]}
    return case


def _review_fixture(root: Path, count: int = 2, *, category: str = "lookup") -> tuple[Path, Path, Path, tuple[str, ...]]:
    host = SyntheticHost(count)
    code, output, errors = _run(host, root, root / "draft", count=max(1, count), category=category)
    assert (code, errors) == (0, ""), (output, errors)
    manifest_path = root / "draft/manifest.json"
    marks_path = root / "marks.txt"
    provenance = json.loads(manifest_path.read_bytes())
    marks_path.write_text(
        f"synthetic: CSA-1 {provenance['sheet_sha256'][:manifest.DIGEST_PREFIX_CHARS]} 2099-01-04\n"
        + "".join(f"{ordinal:02d} accept\n" for ordinal in range(1, count + 1)),
        encoding="utf-8",
    )
    return root / "eval-v9", manifest_path, marks_path, tuple(host.court_map.courts)


def _run_intake(manifest_path: Path, marks_path: Path | None, known_courts: tuple[str, ...] | None,
                *, set_root: Path | None = None, dry_run: bool = False, template: bool = False) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    args = [str(manifest_path)]
    if marks_path is not None:
        args.append(str(marks_path))
    if template:
        args.append("--template")
    if set_root is not None:
        args.extend(("--set", str(set_root)))
    if dry_run:
        args.append("--dry-run")
    with contextlib.redirect_stderr(errors):
        code = intake.main(args, court_ids=known_courts, stdout=output, stderr=errors)
    return code, output.getvalue(), errors.getvalue()


def _rewrite_draft(manifest_path: Path, ordinal: int, mutate: Callable[[dict[str, object]], None]) -> None:
    provenance = json.loads(manifest_path.read_bytes())
    path = manifest_path.parent / "drafts.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    value = json.loads(lines[ordinal - 1])
    mutate(value)
    lines[ordinal - 1] = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    path.write_bytes(b"".join(lines))
    provenance["entries"][ordinal - 1]["draft_sha256"] = hashlib.sha256(lines[ordinal - 1]).hexdigest()
    manifest_path.write_text(json.dumps(provenance), encoding="utf-8")


class Intake(unittest.TestCase):
    """A digest-held CSA review admits a sampled or fully read batch atomically."""

    def assert_private_absent(self, text: str) -> None:
        for private in (QUESTION, ANSWER, CLOSED, REASON, PASSAGE, CASE_NAME):
            self.assertNotIn(private, text)

    def assert_refusal(self, result: tuple[int, str, str], *expected: str) -> None:
        code, output, errors = result
        self.assertEqual((code, output), (1, ""))
        for text in expected:
            self.assertIn(text, errors)
        self.assertIn("Fix:", errors)
        self.assert_private_absent(output + errors)

    def test_old_formats_refuse_with_draft_command_and_format_three_appends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            provenance = json.loads(manifest_path.read_bytes())
            self.assertEqual(provenance["format"], 3)
            path = set_root / synthetic.case_path("lookup")
            for old_format in (1, 2):
                with self.subTest(format=old_format):
                    provenance["format"] = old_format
                    manifest_path.write_text(json.dumps(provenance))
                    self.assert_refusal(
                        _run_intake(manifest_path, marks_path, known_courts),
                        "manifest: refused", "format", "Draft this category again", "python3 -m tools.synthetic.draft",
                    )
                    self.assertFalse(path.exists())
            provenance["format"] = manifest.MANIFEST_FORMAT
            manifest_path.write_text(json.dumps(provenance))
            code, output, errors = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(len(path.read_bytes().splitlines()), len(provenance["entries"]))

    def test_discard_triples_allow_shared_sources_edges_and_kinds_with_kept_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            provenance = json.loads(manifest_path.read_bytes())
            source = provenance["entries"][0]["source_id"]
            provenance["discards"] = [
                {"reason": "same-document", "source_id": source, "edge": edge, "kind": kind}
                for edge in (None, "a" * 64, "b" * 64)
                for kind in (None, *synthetic.TREATMENT_KINDS, *synthetic.ATTRIBUTION_KINDS)
            ]
            provenance["discards"].append({**provenance["discards"][0], "source_id": provenance["entries"][1]["source_id"]})
            manifest_path.write_text(json.dumps(provenance))
            code, output, errors = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((code, errors), (0, ""), output)
            records = [json.loads(line) for line in (set_root / synthetic.case_path("lookup")).read_bytes().splitlines()]
            self.assertEqual([entry["gold"][0]["source_id"] for entry in records],
                             [entry["source_id"] for entry in provenance["entries"]])
            self.assert_private_absent(output)

    def test_duplicate_discard_triples_refuse_even_with_different_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            provenance = json.loads(manifest_path.read_bytes())
            for edge, kind in ((None, None), ("a" * 64, synthetic.TREATMENT_KINDS[0]),
                               (None, synthetic.ATTRIBUTION_KINDS[0])):
                with self.subTest(edge=edge, kind=kind):
                    entry = {"reason": "same-document", "source_id": "fictitious/discard", "edge": edge, "kind": kind}
                    provenance["discards"] = [entry, {**entry, "reason": "same-cluster"}]
                    manifest_path.write_text(json.dumps(provenance))
                    self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest: refused", "discards")
                    self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_attribution_manifest_accepts_both_discarded_kinds_of_one_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory), count=0)
            provenance = json.loads(manifest_path.read_bytes())
            source = "fictitious/discard"
            provenance["category"] = "section-attribution"
            provenance["template"] = "section-attribution@1"
            provenance["template_digest"] = synthetic.TEMPLATE_DIGESTS[provenance["template"]]
            provenance["entries"] = []
            provenance["discards"] = [
                {"reason": "off-section", "source_id": source, "edge": None, "kind": kind}
                for kind in synthetic.ATTRIBUTION_KINDS
            ]
            manifest_path.write_text(json.dumps(provenance))
            (manifest_path.parent / "drafts.jsonl").write_bytes(b"")
            marks_path.write_text(marks_path.read_text().splitlines()[0] + "\n")
            code, output, errors = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertIn("summary: accepted 0, admitted unread 0, rejected 0", output)
            self.assertFalse((set_root / synthetic.case_path("section-attribution")).exists())

    def test_discard_key_order_missing_extra_and_value_refusals(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("reason", "unknown"), ("reason", None), ("reason", []),
            ("source_id", None), ("source_id", ""), ("source_id", "bad source"),
            ("source_id", []), ("source_id", "x" * (indexsample.SOURCE_ID_MAX_LENGTH + 1)),
            ("edge", ""), ("edge", "a" * 63), ("edge", "a" * 65), ("edge", "A" * 64),
            ("edge", "g" * 64), ("edge", 1), ("edge", True), ("edge", []), ("edge", {}),
            ("kind", ""), ("kind", "Negative"), ("kind", "majority"), ("kind", QUESTION),
            ("kind", 1), ("kind", True), ("kind", []), ("kind", {}),
        )
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            provenance = json.loads(manifest_path.read_bytes())
            valid = {"reason": "same-document", "source_id": "fictitious/discard", "edge": None, "kind": None}
            malformed: list[object] = [None, [], dict(reversed(tuple(valid.items()))), {**valid, "extra": QUESTION}]
            malformed.extend({key: value for key, value in valid.items() if key != missing} for missing in manifest.DISCARD_KEYS)
            malformed.extend({**valid, key: value} for key, value in cases)
            for index, entry in enumerate(malformed):
                with self.subTest(index=index):
                    provenance["discards"] = [entry]
                    manifest_path.write_text(json.dumps(provenance))
                    self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest: refused", "discards")
                    self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_each_discard_reason_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            provenance = json.loads(manifest_path.read_bytes())
            provenance["discards"] = [
                {"reason": reason, "source_id": f"fictitious/discard-{index}", "edge": None, "kind": None}
                for index, reason in enumerate(manifest.DISCARD_REASONS)
            ]
            manifest_path.write_text(json.dumps(provenance))
            code, output, errors = _run_intake(manifest_path, marks_path, known_courts, dry_run=True)
            self.assertEqual((code, errors), (0, ""), output)

    def test_kept_entries_remain_unique_by_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            provenance = json.loads(manifest_path.read_bytes())
            provenance["entries"][1]["source_id"] = provenance["entries"][0]["source_id"]
            manifest_path.write_text(json.dumps(provenance))
            self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest: refused", "entry 02")
            self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_template_matches_draft_with_no_other_inputs_or_writes(self) -> None:
        for category in ("lookup", "retrieval"):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _set, manifest_path, marks_path, _courts = _review_fixture(root, synthetic.READ_SHARE + 1, category=category)
                expected = (manifest_path.parent / "marks.template.txt").read_text()
                for name in ("marks.template.txt", "sheet.txt", "drafts.jsonl"):
                    (manifest_path.parent / name).unlink()
                marks_path.unlink()
                before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                with patch.object(intake, "write_atomic") as write, patch.object(evalset, "load_set") as load:
                    result = _run_intake(manifest_path, None, None, template=True)
                self.assertEqual(result, (0, expected, ""))
                write.assert_not_called()
                load.assert_not_called()
                self.assertEqual({path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)

    def test_marks_and_template_cli_combinations_are_usage_errors(self) -> None:
        for marks, template, message in (
            (None, False, "marks are required unless --template is used"),
            (Path("fictitious-marks"), True, "marks must be omitted with --template"),
        ):
            with self.subTest(template=template), patch.object(intake, "_load_manifest") as load:
                result = _run_intake(Path("fictitious-manifest"), marks, (), template=template)
                self.assertEqual(result[:2], (2, ""))
                self.assertIn(message, result[2])
                load.assert_not_called()

    def test_old_format_refuses_before_key_order_and_names_a_new_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _set, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            value = json.loads(manifest_path.read_bytes())
            value["format"] = 1
            for key in ("read_share", "read_ordinals", "sheet_bytes"):
                del value[key]
            manifest_path.write_text(json.dumps(dict(reversed(tuple(value.items())))))
            result = _run_intake(manifest_path, marks_path, known_courts)
            self.assert_refusal(result, "manifest format is unsupported", "Draft this category again with python3 -m tools.synthetic.draft")

    def test_sample_marks_extras_unread_reviews_and_dry_run_rows(self) -> None:
        for category in ("retrieval", "synthesis", "existence"):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                set_root, manifest_path, marks_path, known_courts = _review_fixture(root, 7, category=category)
                header = marks_path.read_text().splitlines()[0]
                marks_path.write_text(header + "\n01 accept\n06 accept\n02 reject\n03 accept\n")
                before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                dry = _run_intake(manifest_path, marks_path, known_courts, dry_run=True)
                self.assertEqual((dry[0], dry[2]), (0, ""), dry[1])
                self.assertEqual({path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)
                real = _run_intake(manifest_path, marks_path, known_courts)
                self.assertEqual(real, dry)
                payload = (set_root / synthetic.case_path(category)).read_bytes()
                records = [json.loads(line) for line in payload.splitlines()]
                self.assertEqual([row["review"]["read"] for row in records], [True, True, False, False, True, False])
                self.assertTrue(all(tuple(row["review"]) == ("by", "on", "read") for row in records))
                self.assertEqual(real[1].splitlines(), [
                    f"01: accepted research-qa-{category}-001 closed-book-answerable", "02: rejected",
                    f"03: accepted research-qa-{category}-002 closed-book-answerable",
                    f"04: admitted unread research-qa-{category}-003 closed-book-answerable",
                    f"05: admitted unread research-qa-{category}-004 closed-book-answerable",
                    f"06: accepted research-qa-{category}-005 closed-book-answerable",
                    f"07: admitted unread research-qa-{category}-006 closed-book-answerable",
                    f"cases: 6 lines sha256 {hashlib.sha256(payload).hexdigest()}",
                    f"held out: 3 of 6 cases (share 1/{synthetic.HELD_OUT_SHARE}, 0 forced by family)",
                    _held_out_pin(set_root, category),
                    "summary: accepted 3, admitted unread 3, rejected 1, closed-book-answerable 6",
                    f"read: 4 of 7 cases (share 1/{synthetic.READ_SHARE}), rejected 1",
                    "set digest: unchanged since the draft",
                ])
                self.assertTrue(evalset.load_set(set_root, known_courts).ok)
                self.assert_private_absent(real[1] + real[2])

    def test_missing_sample_mark_is_located_and_tampered_sample_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory), 7, category="retrieval")
            header = marks_path.read_text().splitlines()[0]
            marks_path.write_text(header + "\n01 accept\n")
            result = _run_intake(manifest_path, marks_path, known_courts)
            self.assert_refusal(result, "line 3: ordinal 06 is unmarked", "Mark every read ordinal")
            original = json.loads(manifest_path.read_bytes())
            for changes in ({"read_ordinals": [1]}, {"read_share": 1}, {"read_ordinals": [1, True]}):
                manifest_path.write_text(json.dumps({**original, **changes}))
                self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest: refused")
            self.assertFalse((set_root / synthetic.case_path("retrieval")).exists())

    def test_ceiling_uses_sample_rejections_and_full_read_is_the_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory), 6, category="retrieval")
            header = marks_path.read_text().splitlines()[0]
            marks_path.write_text(header + "\n01 reject\n06 accept\n02 accept\n03 accept\n04 accept\n")
            with patch.object(intake, "write_atomic") as write:
                result = _run_intake(manifest_path, marks_path, known_courts)
            write.assert_not_called()
            self.assertEqual(result, (1, "",
                "marks: refused — 1 of 2 read cases rejected exceeds the ceiling. "
                "Fix: Read every case and mark every ordinal accept or reject, then retry.\n"))
            self.assertFalse((set_root / synthetic.case_path("retrieval")).exists())
            marks_path.write_text(marks_path.read_text() + "05 accept\n")
            result = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            self.assertIn("summary: accepted 5, admitted unread 0, rejected 1", result[1])
            self.assertIn(f"read: 6 of 6 cases (share 1/{synthetic.READ_SHARE}), rejected 1", result[1])
            records = [json.loads(line) for line in (set_root / synthetic.case_path("retrieval")).read_bytes().splitlines()]
            self.assertTrue(all(row["review"]["read"] for row in records))

    def test_sample_rejection_ceiling_is_inclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            kept = synthetic.READ_SHARE * (synthetic.SAMPLE_REJECTION_CEILING - 1) + 1
            _set, manifest_path, marks_path, known_courts = _review_fixture(Path(directory), kept, category="retrieval")
            header = marks_path.read_text().splitlines()[0]
            ordinals = json.loads(manifest_path.read_bytes())["read_ordinals"]
            marks_path.write_text(header + "\n" + "".join(
                f"{ordinal:02d} {'reject' if ordinal == ordinals[0] else 'accept'}\n" for ordinal in ordinals
            ))
            result = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            self.assertIn(f"read: {len(ordinals)} of {kept} cases (share 1/{synthetic.READ_SHARE}), rejected 1", result[1])

    def test_empty_batch_accepts_header_alone_and_writes_nothing(self) -> None:
        for category in ("lookup", "retrieval"):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _set, manifest_path, marks_path, known_courts = _review_fixture(root, 0, category=category)
                before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                with patch.object(intake, "write_atomic") as write:
                    result = _run_intake(manifest_path, marks_path, known_courts)
                self.assertEqual((result[0], result[2]), (0, ""), result[1])
                share = 1 if category == "lookup" else synthetic.READ_SHARE
                self.assertEqual(result[1].splitlines(), [
                    f"cases: 0 lines sha256 {hashlib.sha256(b'').hexdigest()}",
                    f"held out: 0 of 0 cases (share 1/{synthetic.HELD_OUT_SHARE}, 0 forced by family)",
                    "summary: accepted 0, admitted unread 0, rejected 0, closed-book-answerable 0",
                    f"read: 0 of 0 cases (share 1/{share}), rejected 0", "set digest: unchanged since the draft",
                ])
                write.assert_not_called()
                self.assertEqual({path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)
                value = json.loads(manifest_path.read_bytes())
                value["sheet_bytes"] = 1
                manifest_path.write_text(json.dumps(value))
                self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest: refused")

    def test_drafts_and_sheet_allow_extra_suffixes_but_hold_the_recorded_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            for name, suffix in (("drafts.jsonl", b"fictitious orphan line without newline"), ("sheet.txt", b"\xff fictitious orphan block")):
                path = manifest_path.parent / name
                path.write_bytes(path.read_bytes() + suffix)
            result = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            self.assertEqual(len((set_root / synthetic.case_path("lookup")).read_bytes().splitlines()), 2)

    def test_sheet_prefix_length_and_digest_are_held(self) -> None:
        for scenario in ("short", "smaller-count", "larger-count"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
                value = json.loads(manifest_path.read_bytes())
                if scenario == "short":
                    path = manifest_path.parent / "sheet.txt"
                    path.write_bytes(path.read_bytes()[:-1])
                else:
                    value["sheet_bytes"] += -1 if scenario == "smaller-count" else 1
                    manifest_path.write_text(json.dumps(value))
                marks_path.unlink()
                self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "drafts: refused", "sheet digest differs")
                self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_interrupted_replacements_recover_from_the_manifest_and_template_on_disk(self) -> None:
        for failed_name in ("manifest.json", "marks.template.txt"):
            with self.subTest(failed_name=failed_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                out = root / "draft"
                host = SyntheticHost(2)
                original = draft.write_atomic
                interrupted: dict[str, bytes] = {}

                def write(
                    path: Path, payload: bytes, *, owner: tuple[int, int] | None = None,
                    failed_name: str = failed_name, out: Path = out,
                    interrupted: dict[str, bytes] = interrupted, original: Callable[..., None] = original,
                ) -> None:
                    if path.name == failed_name:
                        value = json.loads(payload if failed_name == "manifest.json" else (out / "manifest.json").read_bytes())
                        if len(value["entries"]) == 2:
                            interrupted["payload"] = payload
                            interrupted["template"] = (out / "marks.template.txt").read_bytes()
                            raise OSError("Fictitious replacement failed")
                    original(path, payload, owner=owner)

                with patch.object(draft, "write_atomic", side_effect=write):
                    code, output, errors = _run(host, root, out, category="retrieval")
                self.assertEqual(code, 1)
                self.assertIn("write: refused", errors)
                self.assertIn("Fix:", errors)
                self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
                self.assertTrue(interrupted)
                self.assertEqual((out / "marks.template.txt").read_bytes(), interrupted["template"])
                manifest_path = out / "manifest.json"
                value = json.loads(manifest_path.read_bytes())
                self.assertEqual(len((out / "drafts.jsonl").read_bytes().splitlines()), 2)
                sheet = (out / "sheet.txt").read_bytes()
                self.assertIn(b"Synthetic case 02 optional", sheet)
                marks_path = root / "marks.txt"
                marks_path.write_text(interrupted["template"].decode()
                                      .replace("<CSA role id>", "CSA-1").replace("<ISO date>", "2099-01-04")
                                      .replace(" \n", " accept\n"))
                known_courts = tuple(str(row["court"]) for row in host.documents)
                target = root / "eval-v9" / synthetic.case_path("retrieval")
                if failed_name == "manifest.json":
                    self.assertEqual(len(value["entries"]), 1)
                    self.assertGreater(len(sheet), value["sheet_bytes"])
                    self.assertEqual(hashlib.sha256(sheet[:value["sheet_bytes"]]).hexdigest(), value["sheet_sha256"])
                else:
                    self.assertEqual(len(value["entries"]), 2)
                    self.assertEqual(len(sheet), value["sheet_bytes"])
                    result = _run_intake(manifest_path, marks_path, known_courts)
                    self.assert_refusal(result, "line 1: sheet digest prefix differs",
                                        "python3 -m tools.synthetic.intake <manifest> --template")
                    self.assertFalse(target.exists())
                    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                    regenerated = _run_intake(manifest_path, None, None, template=True)
                    self.assertEqual(regenerated, (0, interrupted["payload"].decode(), ""))
                    self.assertEqual({path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)
                    marks_path.write_text(regenerated[1]
                                          .replace("<CSA role id>", "CSA-1").replace("<ISO date>", "2099-01-04")
                                          .replace(" \n", " accept\n"))
                result = _run_intake(manifest_path, marks_path, known_courts)
                self.assertEqual((result[0], result[2]), (0, ""), result[1])
                records = [json.loads(line) for line in target.read_bytes().splitlines()]
                self.assertEqual(len(records), len(value["entries"]))
                self.assertEqual([row["gold"][0]["source_id"] for row in records],
                                 [entry["source_id"] for entry in value["entries"]])
                self.assertEqual([row["review"]["read"] for row in records],
                                 [True] if failed_name == "manifest.json" else [True, False])
                self.assertTrue(evalset.load_set(root / "eval-v9", known_courts).ok)
                self.assert_private_absent(output + errors + result[1] + result[2])

    def test_dry_run_real_append_pin_pair_and_repeated_intake(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            path = set_root / synthetic.case_path("lookup")
            before = {file.relative_to(set_root): file.read_bytes() for file in set_root.rglob("*") if file.is_file()}
            with patch.object(intake, "write_atomic") as no_write:
                dry = _run_intake(manifest_path, marks_path, known_courts, dry_run=True)
            no_write.assert_not_called()
            self.assertEqual((dry[0], dry[2]), (0, ""), dry[1])
            self.assertFalse(path.exists())
            self.assertEqual({file.relative_to(set_root): file.read_bytes() for file in set_root.rglob("*") if file.is_file()}, before)
            with patch.object(engine, "call_model_server") as call:
                real = _run_intake(manifest_path, marks_path, known_courts)
            call.assert_not_called()
            self.assertEqual(real, dry)
            payload = path.read_bytes()
            records = [json.loads(line) for line in payload.splitlines()]
            self.assertEqual([row["id"] for row in records], ["research-qa-lookup-001", "research-qa-lookup-002"])
            self.assertTrue(payload.isascii())
            self.assertTrue(all(tuple(row) == synthetic.CASE_KEYS for row in records))
            self.assertTrue(all(row["review"] == {"by": "CSA-1", "on": "2099-01-04"} for row in records))
            self.assertEqual(real[1].splitlines(), [
                "01: accepted research-qa-lookup-001 closed-book-answerable",
                "02: accepted research-qa-lookup-002 closed-book-answerable",
                f"cases: 2 lines sha256 {hashlib.sha256(payload).hexdigest()}",
                f"held out: 1 of 2 cases (share 1/{synthetic.HELD_OUT_SHARE}, 0 forced by family)",
                _held_out_pin(set_root, "lookup"),
                "summary: accepted 2, admitted unread 0, rejected 0, closed-book-answerable 2",
                "read: 2 of 2 cases (share 1/1), rejected 0",
                "set digest: unchanged since the draft",
            ])
            loaded = evalset.load_set(set_root, known_courts)
            self.assertTrue(loaded.ok, loaded.findings)
            assert loaded.loaded is not None
            self.assertEqual(loaded.loaded.unsigned_ids, frozenset())
            self.assert_private_absent(real[1])
            self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "ordinal 01", "already cased")
            self.assertEqual(path.read_bytes(), payload)

    def test_marks_are_intaken_in_ordinal_order_and_all_reject_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            path = set_root / synthetic.case_path("lookup")
            header = marks_path.read_text().splitlines()[0]
            marks_path.write_text(header + "\n02 reject\n01 reject\n")
            code, output, errors = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertFalse(path.exists())
            self.assertEqual(output.splitlines()[:2], ["01: rejected", "02: rejected"])
            marks_path.write_text(header + "\n02 accept\n01 reject\n")
            code, output, errors = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(output.splitlines()[:2], ["01: rejected", "02: accepted research-qa-lookup-001 closed-book-answerable"])
            provenance = json.loads(manifest_path.read_bytes())
            self.assertEqual(json.loads(path.read_bytes())["gold"][0]["source_id"], provenance["entries"][1]["source_id"])

    def test_header_role_digest_date_and_marks_refusals_are_located_and_atomic(self) -> None:
        cases = (
            ("header", "line 1: header is invalid"), ("empty", "line 1: header is invalid"),
            ("attorney", "line 1: reviewer must be a CSA role id"), ("role", "line 1: reviewer must be a CSA role id"),
            ("digest", "line 1: sheet digest prefix differs"), ("short-digest", "line 1: sheet digest prefix differs"),
            ("date", "line 1: review date is invalid"), ("mark", "line 3: mark must"),
            ("duplicate", "line 3: ordinal is marked twice"), ("outside", "line 3: ordinal is outside"),
            ("zero", "line 3: ordinal is outside"), ("missing", "line 3: ordinal 02 is unmarked"),
        )
        for scenario, expected in cases:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
                text = marks_path.read_text()
                header = text.splitlines()[0]
                if scenario == "header":
                    text = "Invalid fictitious header\n01 accept\n02 accept\n"
                elif scenario == "empty":
                    text = ""
                elif scenario == "attorney":
                    text = text.replace("CSA-1", "CHU-attorney-1")
                elif scenario == "role":
                    text = text.replace("CSA-1", "CSA-0")
                elif scenario in {"digest", "short-digest"}:
                    prefix = header.split()[2]
                    replacement = prefix[:-1] if scenario == "short-digest" else ("0" if prefix[0] != "0" else "1") + prefix[1:]
                    text = text.replace(prefix, replacement)
                elif scenario == "date":
                    text = text.replace("2099-01-04", "2099-02-30")
                else:
                    text = header + "\n" + {
                        "mark": "01 accept\n02 revise\n", "duplicate": "01 accept\n01 reject\n02 accept\n",
                        "outside": "01 accept\n03 reject\n", "zero": "01 accept\n00 reject\n", "missing": "01 accept\n",
                    }[scenario]
                marks_path.write_text(text)
                result = _run_intake(manifest_path, marks_path, known_courts)
                self.assert_refusal(result, "marks: refused", expected)
                if scenario in {"attorney", "role"}:
                    self.assertIn("CSA", result[2].split("Fix:", 1)[1])
                self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_edited_sheet_and_draft_are_refused_by_digest_before_reading_marks(self) -> None:
        for changed in ("sheet.txt", "drafts.jsonl"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
                path = manifest_path.parent / changed
                path.write_bytes(path.read_bytes().replace(QUESTION.encode(), b"Changed fictitious question"))
                marks_path.unlink()
                result = _run_intake(manifest_path, marks_path, known_courts)
                self.assert_refusal(result, "drafts: refused", "digest differs from the manifest")
                self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_draft_count_keys_and_facts_are_held(self) -> None:
        for scenario in ("count", "extra", "ordinal", "source", "quote", "labels", "closed-book", "json"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
                if scenario == "count":
                    path = manifest_path.parent / "drafts.jsonl"
                    path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
                elif scenario == "json":
                    provenance = json.loads(manifest_path.read_bytes())
                    path = manifest_path.parent / "drafts.jsonl"
                    lines = path.read_bytes().splitlines(keepends=True)
                    lines[1] = b"{ invalid fictitious JSON\n"
                    path.write_bytes(b"".join(lines))
                    provenance["entries"][1]["draft_sha256"] = hashlib.sha256(lines[1]).hexdigest()
                    manifest_path.write_text(json.dumps(provenance))
                else:
                    def mutate(value: dict[str, object], mode: str = scenario) -> None:
                        if mode == "extra":
                            value["extra"] = QUESTION
                        elif mode == "ordinal":
                            value["ordinal"] = 1
                        elif mode in {"source", "quote"}:
                            gold = cast(list[dict[str, object]], value["gold"])
                            gold[0]["source_id" if mode == "source" else "quote"] = "fictitious/other" if mode == "source" else QUESTION
                        elif mode == "labels":
                            value["labels"] = [synthetic.SYNTHETIC_ORIGIN]
                        else:
                            value["closed_book_answer"] = ""
                    _rewrite_draft(manifest_path, 2, mutate)
                self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "drafts: refused")
                self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_manifest_shape_refusals(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("format", True), ("format", 999), ("set_root", "relative"), ("set_digest", "bad"),
            ("category", "retrieval"), ("template", "lookup@999"), ("template_digest", "a" * 64),
            ("reading", "unknown@1"), ("reading_digest", "a" * 64), ("generation", "bad"),
            ("seed", True), ("asked", 0), ("asked", 1), ("entries", None), ("discards", {}),
            ("sheet_sha256", "bad"), ("case_pin", [True, "a" * 64]), ("case_pin", [-1, "a" * 64]),
            ("case_pin", [1, "bad"]), ("case_pin", [1]),
            ("read_share", True), ("read_share", 0), ("read_share", synthetic.READ_SHARE),
            ("read_ordinals", None), ("read_ordinals", []), ("read_ordinals", [1]),
            ("read_ordinals", [True, 2]), ("read_ordinals", [1.0, 2]), ("read_ordinals", [2, 1]),
            ("read_ordinals", [1, 1]), ("read_ordinals", [1, 3]),
            ("sheet_bytes", None), ("sheet_bytes", True), ("sheet_bytes", -1), ("sheet_bytes", 0),
        )
        for key, value in cases:
            with self.subTest(key=key, kind=type(value).__name__), tempfile.TemporaryDirectory() as directory:
                set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
                provenance = json.loads(manifest_path.read_bytes())
                provenance[key] = value
                manifest_path.write_text(json.dumps(provenance))
                self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest: refused")
                self.assertFalse((set_root / synthetic.case_path("lookup")).exists())
        for scenario in ("keys", "order", "entry-keys", "entry-order", "discards"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
                provenance = json.loads(manifest_path.read_bytes())
                if scenario == "keys":
                    provenance["extra"] = QUESTION
                elif scenario == "order":
                    provenance = dict(reversed(tuple(provenance.items())))
                elif scenario == "entry-keys":
                    provenance["entries"][1]["extra"] = QUESTION
                elif scenario == "entry-order":
                    provenance["entries"][1] = dict(reversed(tuple(provenance["entries"][1].items())))
                else:
                    provenance["discards"] = [{"reason": "unknown", "source_id": "fictitious/opinion", "edge": None, "kind": None}]
                manifest_path.write_text(json.dumps(provenance))
                self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest: refused")

    def test_manifest_entry_field_refusals(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("ordinal", True), ("ordinal", 1), ("source_id", "bad source"),
            ("sha256", "bad"), ("doc_id", "bad"), ("start", -1), ("end", False),
            ("length", 1), ("quote_sha256", "bad"), ("draft_sha256", "bad"),
            ("closed_book_score", None), ("closed_book_score", True), ("closed_book_score", 4),
            ("label", None), ("label", "unknown"),
        )
        for key, value in cases:
            with self.subTest(key=key, kind=type(value).__name__), tempfile.TemporaryDirectory() as directory:
                _set, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
                provenance = json.loads(manifest_path.read_bytes())
                provenance["entries"][1][key] = value
                manifest_path.write_text(json.dumps(provenance))
                self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest: refused")

    def test_loader_refused_case_never_reaches_a_write(self) -> None:
        for field_name, value in (("question", QUESTION + "\nSecond line"), ("jurisdiction", ["unknowncourt"])):
            with self.subTest(field=field_name), tempfile.TemporaryDirectory() as directory:
                set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
                def mutate_case(row: dict[str, object], key: str = field_name, replacement: object = value) -> None:
                    row[key] = replacement
                _rewrite_draft(manifest_path, 2, mutate_case)
                with patch.object(intake, "write_atomic") as write:
                    result = _run_intake(manifest_path, marks_path, known_courts)
                write.assert_not_called()
                self.assert_refusal(result, "validate: refused", "ordinal 02")
                self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_ids_continue_and_the_file_pin_allows_appends_between_intakes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, manifest_path, marks_path, known_courts = _review_fixture(root)
            header = marks_path.read_text().splitlines()[0]
            marks_path.write_text(header + "\n01 accept\n02 reject\n")
            first = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((first[0], first[2]), (0, ""), first[1])
            path = set_root / synthetic.case_path("lookup")
            original = path.read_bytes()
            host = SyntheticHost(3)
            known_courts = tuple(str(row["court"]) for row in host.documents)
            code, output, errors = _run(host, root, root / "batch-two", count=2)
            self.assertEqual((code, errors), (0, ""), output)
            manifest_path = root / "batch-two/manifest.json"
            provenance = json.loads(manifest_path.read_bytes())
            header = f"synthetic: CSA-2 {provenance['sheet_sha256'][:manifest.DIGEST_PREFIX_CHARS]} 2099-01-05"
            marks_path.write_text(header + "\n01 accept\n02 reject\n")
            second = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((second[0], second[2]), (0, ""), second[1])
            self.assertIn("accepted research-qa-lookup-002", second[1])
            self.assertTrue(path.read_bytes().startswith(original))
            marks_path.write_text(header + "\n01 reject\n02 accept\n")
            third = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((third[0], third[2]), (0, ""), third[1])
            self.assertIn("accepted research-qa-lookup-003", third[1])
            self.assertIn("set digest: moved since the draft", third[1])
            records = [json.loads(line) for line in path.read_bytes().splitlines()]
            self.assertEqual([row["id"] for row in records], [f"research-qa-lookup-{i:03d}" for i in range(1, 4)])
            self.assertEqual(records[-1]["review"], {"by": "CSA-2", "on": "2099-01-05"})

    def test_pin_edit_refuses_but_set_override_and_unrelated_changes_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, manifest_path, marks_path, known_courts = _review_fixture(root)
            first = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((first[0], first[2]), (0, ""), first[1])
            host = SyntheticHost(3)
            known_courts = tuple(str(row["court"]) for row in host.documents)
            code, output, errors = _run(host, root, root / "next", count=1)
            self.assertEqual((code, errors), (0, ""), output)
            manifest_path = root / "next/manifest.json"
            provenance = json.loads(manifest_path.read_bytes())
            marks_path.write_text(f"synthetic: CSA-1 {provenance['sheet_sha256']} 2099-01-04\n01 accept\n")
            path = set_root / synthetic.case_path("lookup")
            previous = path.read_bytes()
            records = [json.loads(line) for line in previous.splitlines()]
            records[0]["notes"] = "Fictitious edit after the draft"
            path.write_bytes(b"".join(synthetic.case_line(row).encode() for row in records))
            changed = path.read_bytes()
            self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "category file changed")
            self.assertEqual(path.read_bytes(), changed)
            path.write_bytes(previous)
            override = root / "override/eval-v9"
            shutil.copytree(set_root, override)
            unrelated = _synthetic_case("research-qa-lookup-099")
            unrelated["gold"] = [{**cast(list[dict[str, object]], unrelated["gold"])[0], "source_id": "fictitious/unrelated"}]
            unrelated["cluster_id"] = "fictitious/unrelated"
            unrelated["expected"] = {"answer": ANSWER, "must_cite": ["fictitious/unrelated"]}
            unrelated["jurisdiction"] = [known_courts[0]]
            (override / "research-qa/other.jsonl").write_text(synthetic.case_line(unrelated))
            result = _run_intake(manifest_path, marks_path, known_courts, set_root=override)
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            self.assertIn("set digest: moved since the draft", result[1])
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(len((override / synthetic.case_path("lookup")).read_bytes().splitlines()), 3)

    def test_reload_findings_restore_existing_bytes_or_remove_a_new_file(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                set_root, manifest_path, marks_path, known_courts = _review_fixture(root)
                path = set_root / synthetic.case_path("lookup")
                if existing:
                    header = marks_path.read_text().splitlines()[0]
                    marks_path.write_text(header + "\n01 accept\n02 reject\n")
                    first = _run_intake(manifest_path, marks_path, known_courts)
                    self.assertEqual((first[0], first[2]), (0, ""), first[1])
                    marks_path.write_text(header + "\n01 reject\n02 accept\n")
                previous = path.read_bytes() if existing else None
                other = _synthetic_case(f"research-qa-lookup-{2 if existing else 1:03d}")
                other["gold"] = [{**cast(list[dict[str, object]], other["gold"])[0], "source_id": "fictitious/other-document"}]
                other["cluster_id"] = "fictitious/other-document"
                other["expected"] = {"answer": ANSWER, "must_cite": ["fictitious/other-document"]}
                other["jurisdiction"] = [known_courts[0]]
                (set_root / "research-qa").mkdir(exist_ok=True)
                other_path = set_root / "research-qa/other.jsonl"
                other_path.write_text(synthetic.case_line(other))
                other_bytes = other_path.read_bytes()
                self.assertTrue(evalset.load_set(set_root, known_courts).ok)
                result = _run_intake(manifest_path, marks_path, known_courts)
                self.assert_refusal(result, "eval-set reload", "previous files restored", path.relative_to(set_root).as_posix())
                self.assertEqual(path.read_bytes() if path.exists() else None, previous)
                self.assertEqual(other_path.read_bytes(), other_bytes)
                self.assertTrue(evalset.load_set(set_root, known_courts).ok)

    def test_failed_write_and_reload_exception_leave_no_appended_case(self) -> None:
        for scenario in ("write", "reload"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
                if scenario == "write":
                    with patch.object(intake, "write_atomic", side_effect=OSError(REASON)):
                        result = _run_intake(manifest_path, marks_path, known_courts)
                else:
                    loaded = evalset.load_set(set_root, known_courts)
                    with patch.object(evalset, "load_set", side_effect=[loaded, OSError(REASON)]):
                        result = _run_intake(manifest_path, marks_path, known_courts)
                self.assert_refusal(result)
                self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_non_sample_manifest_and_missing_court_map_or_wrong_set_version_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, manifest_path, marks_path, known_courts = _review_fixture(root)
            provenance = json.loads(manifest_path.read_bytes())
            provenance["generation"] = provenance["generation"].removesuffix(f"__{indexsample.SAMPLE_SUFFIX}")
            manifest_path.write_text(json.dumps(provenance))
            self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest: refused")
            provenance["generation"] = indexsample.sample_name(provenance["generation"])
            manifest_path.write_text(json.dumps(provenance))
            with patch.object(courts, "load_court_map", return_value=courts.CourtMapLoadResult()):
                self.assert_refusal(_run_intake(manifest_path, marks_path, None), "court map cannot be loaded", "courts.yaml")
            self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts, set_root=root / "eval-v10"), "set version differs")
            self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_set_findings_missing_inputs_and_real_command_court_map_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, manifest_path, marks_path, known_courts = _review_fixture(Path(directory))
            fake_map = cast(courts.CourtMap, SimpleNamespace(courts=dict.fromkeys(known_courts)))
            with patch.object(courts, "load_court_map", return_value=courts.CourtMapLoadResult(court_map=fake_map)):
                result = _run_intake(manifest_path, marks_path, None, dry_run=True)
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            (set_root / "research-qa").mkdir()
            (set_root / "research-qa/broken.jsonl").write_text(QUESTION + "\n")
            self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "set: refused")
            (set_root / "research-qa/broken.jsonl").unlink()
            marks_path.unlink()
            self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "marks cannot be read")
            manifest_path.unlink()
            self.assert_refusal(_run_intake(manifest_path, marks_path, known_courts), "manifest cannot be read")


    def test_held_out_seed_share_file_order_and_second_batch_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, manifest_path, marks_path, known_courts = _review_fixture(root, 6)
            value = json.loads(manifest_path.read_bytes())
            self.assertEqual(intake._load_manifest(manifest_path).seed, value["seed"])
            result = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            path = set_root / synthetic.case_path("lookup")
            sidecar = evalset.held_out_path(path)
            records = [json.loads(line) for line in path.read_bytes().splitlines()]
            chosen = synthetic.held_out_clusters((row["cluster_id"] for row in records), value["seed"])
            self.assertEqual(sidecar.read_text().splitlines(), [row["id"] for row in records if row["cluster_id"] in chosen])
            self.assertEqual(len(chosen), len(records) // synthetic.HELD_OUT_SHARE)
            prefix = sidecar.read_bytes()
            self.assertIn(_held_out_pin(set_root, "lookup"), result[1])
            self.assertLess(result[1].index("cases:"), result[1].index("held out:"))
            host = SyntheticHost(10)
            code, output, errors = _run(host, root, root / "second", count=4)
            self.assertEqual((code, errors), (0, ""), output)
            manifest_path = root / "second/manifest.json"
            value = json.loads(manifest_path.read_bytes())
            marks_path.write_text(f"synthetic: CSA-1 {value['sheet_sha256'][:manifest.DIGEST_PREFIX_CHARS]} 2099-01-04\n"
                                  + "".join(f"{index:02d} accept\n" for index in range(1, 5)))
            result = _run_intake(manifest_path, marks_path, tuple(host.court_map.courts))
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            self.assertTrue(sidecar.read_bytes().startswith(prefix))
            new_records = [json.loads(line) for line in path.read_bytes().splitlines()][len(records):]
            chosen = synthetic.held_out_clusters((row["cluster_id"] for row in new_records), value["seed"])
            expected = [row["id"] for row in new_records if row["cluster_id"] in chosen]
            self.assertEqual(sidecar.read_bytes()[len(prefix):].decode().splitlines(), expected)
            self.assertEqual(len(expected), len(new_records) // synthetic.HELD_OUT_SHARE)
            self.assertIn("0 forced by family", result[1])
            self.assertTrue(evalset.load_set(set_root, host.court_map.courts).ok)

    def test_one_cluster_has_no_sidecar_unless_another_file_already_holds_it_out(self) -> None:
        for forced in (False, True):
            with self.subTest(forced=forced), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                set_root, manifest_path, marks_path, known_courts = _review_fixture(root, 1)
                if forced:
                    value = json.loads((manifest_path.parent / "drafts.jsonl").read_bytes())
                    other = _family_case(value, "retrieval", 1)
                    other_path = set_root / synthetic.case_path("retrieval")
                    other_path.parent.mkdir(parents=True)
                    other_path.write_text(synthetic.case_line(other))
                    other_sidecar = evalset.held_out_path(other_path)
                    other_sidecar.write_text(str(other["id"]) + "\n")
                    previous = other_sidecar.read_bytes()
                result = _run_intake(manifest_path, marks_path, known_courts)
                self.assertEqual((result[0], result[2]), (0, ""), result[1])
                sidecar = evalset.held_out_path(set_root / synthetic.case_path("lookup"))
                self.assertEqual(sidecar.exists(), forced)
                self.assertIn(f"held out: {int(forced)} of 1 cases (share 1/{synthetic.HELD_OUT_SHARE}, {int(forced)} forced by family)", result[1])
                if forced:
                    self.assertEqual(sidecar.read_text().splitlines(), ["research-qa-lookup-001"])
                    self.assertEqual(other_sidecar.read_bytes(), previous)
                    self.assertNotIn("propagated:", result[1])
                else:
                    self.assertNotIn("held-out list:", result[1])
                self.assertTrue(evalset.load_set(set_root, known_courts).ok)

    def test_forced_clusters_are_excluded_from_the_remaining_seeded_draw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, manifest_path, marks_path, known_courts = _review_fixture(root, 4)
            drafts = [json.loads(line) for line in (manifest_path.parent / "drafts.jsonl").read_bytes().splitlines()]
            other = _family_case(drafts[0], "existence", 1)
            path = set_root / synthetic.case_path("existence")
            path.parent.mkdir(parents=True)
            path.write_text(synthetic.case_line(other))
            evalset.held_out_path(path).write_text(str(other["id"]) + "\n")
            value = json.loads(manifest_path.read_bytes())
            chosen = synthetic.held_out_clusters((row["cluster_id"] for row in drafts[1:]), value["seed"])
            result = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual((result[0], result[2]), (0, ""), result[1])
            case_file = set_root / synthetic.case_path("lookup")
            records = [json.loads(line) for line in case_file.read_bytes().splitlines()]
            self.assertEqual(evalset.held_out_path(case_file).read_text().splitlines(), [
                row["id"] for row in records if row["cluster_id"] == other["cluster_id"] or row["cluster_id"] in chosen
            ])
            self.assertIn(f"held out: {1 + len(chosen)} of 4 cases (share 1/{synthetic.HELD_OUT_SHARE}, 1 forced by family)", result[1])
            self.assertTrue(evalset.load_set(set_root, known_courts).ok)

    def test_chosen_clusters_propagate_to_active_cases_across_files_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, manifest_path, marks_path, known_courts = _review_fixture(root, 6)
            drafts = [json.loads(line) for line in (manifest_path.parent / "drafts.jsonl").read_bytes().splitlines()]
            value = json.loads(manifest_path.read_bytes())
            chosen = synthetic.held_out_clusters((row["cluster_id"] for row in drafts), value["seed"])
            expected: dict[str, list[str]] = {}
            for category in ("retrieval", "synthesis"):
                path = set_root / synthetic.case_path(category)
                records = [_family_case(row, category, index) for index, row in enumerate(drafts, 1)]
                retired = _family_case(next(row for row in drafts if row["cluster_id"] in chosen), category, 7)
                successor = _synthetic_case(f"research-qa-{category}-008", category=category)
                successor["jurisdiction"] = [known_courts[0]]
                source = f"fictitious/{category}-previous"
                successor["cluster_id"] = source
                cast(list[dict[str, object]], successor["gold"])[0]["source_id"] = source
                successor["expected"] = {"answer": ANSWER, "must_cite": [source]}
                successor["supersedes"] = retired["id"]
                records.extend((retired, successor))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("".join(synthetic.case_line(row) for row in records))
                if category == "retrieval":
                    evalset.held_out_path(path).write_text(str(successor["id"]) + "\n")
                expected[category] = [cast(str, row["id"]) for row in records[:6] if row["cluster_id"] in chosen]
            before = {path.relative_to(set_root): path.read_bytes() for path in set_root.rglob("*") if path.is_file()}
            dry = _run_intake(manifest_path, marks_path, known_courts, dry_run=True)
            self.assertEqual((dry[0], dry[2]), (0, ""), dry[1])
            self.assertEqual({path.relative_to(set_root): path.read_bytes() for path in set_root.rglob("*") if path.is_file()}, before)
            writes: list[Path] = []
            original_write = intake.write_atomic
            def write(path: Path, payload: bytes, **kwargs: object) -> None:
                writes.append(path)
                original_write(path, payload)
            with patch.object(intake, "write_atomic", side_effect=write):
                result = _run_intake(manifest_path, marks_path, known_courts)
            self.assertEqual(result, dry)
            self.assertEqual(writes, [set_root / synthetic.case_path("lookup"), *(
                evalset.held_out_path(set_root / synthetic.case_path(category)) for category in ("lookup", "retrieval", "synthesis")
            )])
            for category, ids in expected.items():
                sidecar = evalset.held_out_path(set_root / synthetic.case_path(category))
                self.assertEqual(sidecar.read_text().splitlines(), (["research-qa-retrieval-008"] if category == "retrieval" else []) + ids)
                self.assertIn(f"propagated: {category} {len(ids)}\n", result[1])
            list_rows = [row for row in result[1].splitlines() if row.startswith("held-out list:")]
            self.assertEqual(list_rows, [_held_out_pin(set_root, category) for category in ("lookup", "retrieval", "synthesis")])
            self.assertTrue(evalset.load_set(set_root, known_courts).ok)

    def test_unreadable_propagated_sidecar_refuses_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, manifest_path, marks_path, known_courts = _review_fixture(root, 4)
            drafts = [json.loads(line) for line in (manifest_path.parent / "drafts.jsonl").read_bytes().splitlines()]
            path = set_root / synthetic.case_path("retrieval")
            path.parent.mkdir(parents=True)
            path.write_text("".join(synthetic.case_line(_family_case(row, "retrieval", index)) for index, row in enumerate(drafts, 1)))
            sidecar = evalset.held_out_path(path)
            original_read = Path.read_bytes
            def read(path: Path) -> bytes:
                if path == sidecar:
                    raise OSError(REASON)
                return original_read(path)
            before = path.read_bytes()
            with patch.object(Path, "read_bytes", new=read), patch.object(intake, "write_atomic") as write:
                result = _run_intake(manifest_path, marks_path, known_courts)
            write.assert_not_called()
            self.assert_refusal(result, "synthetic-retrieval.held-out.ids", "synthetic-lookup.jsonl")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((set_root / synthetic.case_path("lookup")).exists())

    def test_write_and_reload_failures_restore_every_file_and_remove_new_sidecars(self) -> None:
        for scenario in ("reload-findings", "reload-exception", "sidecar-write", "restore-failure"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                set_root, manifest_path, marks_path, known_courts = _review_fixture(root, 4)
                drafts = [json.loads(line) for line in (manifest_path.parent / "drafts.jsonl").read_bytes().splitlines()]
                for category in ("retrieval", "synthesis"):
                    path = set_root / synthetic.case_path(category)
                    path.parent.mkdir(exist_ok=True)
                    records = [_family_case(row, category, index) for index, row in enumerate(drafts, 1)]
                    if category == "retrieval":
                        prior = _synthetic_case("research-qa-retrieval-005", category="retrieval")
                        prior["jurisdiction"] = [known_courts[0]]
                        records.append(prior)
                        evalset.held_out_path(path).write_text(str(prior["id"]) + "\n")
                    path.write_text("".join(synthetic.case_line(row) for row in records))
                previous_case = _synthetic_case("research-qa-lookup-001")
                previous_case["jurisdiction"] = [known_courts[0]]
                case_path = set_root / synthetic.case_path("lookup")
                case_path.write_text(synthetic.case_line(previous_case))
                own_sidecar = evalset.held_out_path(case_path)
                own_sidecar.write_text(str(previous_case["id"]) + "\n")
                before = {path.relative_to(set_root): path.read_bytes() for path in set_root.rglob("*") if path.is_file()}
                loaded = evalset.load_set(set_root, known_courts)
                self.assertTrue(loaded.ok, loaded.findings)
                original_write = intake.write_atomic
                attempts: list[Path] = []
                def write(path: Path, payload: bytes, *, attempts: list[Path] = attempts,
                          scenario: str = scenario, own_sidecar: Path = own_sidecar,
                          original_write: Callable[..., None] = original_write) -> None:
                    attempts.append(path)
                    if scenario == "sidecar-write" and path.name == "synthetic-synthesis.held-out.ids":
                        raise OSError(REASON)
                    if scenario == "restore-failure" and path == own_sidecar and attempts.count(path) > 1:
                        raise OSError(REASON)
                    original_write(path, payload)
                second: object = OSError(REASON) if scenario == "reload-exception" else evalset.EvalSetLoadResult()
                with (
                    patch.object(intake, "write_atomic", side_effect=write),
                    patch.object(evalset, "load_set", side_effect=[loaded, second]),
                ):
                    result = _run_intake(manifest_path, marks_path, known_courts)
                self.assert_refusal(result, "synthetic-lookup.jsonl", "synthetic-lookup.held-out.ids",
                                    "synthetic-retrieval.held-out.ids", "synthetic-synthesis.held-out.ids")
                after = {path.relative_to(set_root): path.read_bytes() for path in set_root.rglob("*") if path.is_file()}
                if scenario == "restore-failure":
                    self.assertIn("files could not be restored", result[2])
                    self.assertEqual(case_path.read_bytes(), before[case_path.relative_to(set_root)])
                    restored = evalset.held_out_path(set_root / synthetic.case_path("retrieval"))
                    self.assertEqual(restored.read_bytes(), before[restored.relative_to(set_root)])
                    self.assertFalse(evalset.held_out_path(set_root / synthetic.case_path("synthesis")).exists())
                else:
                    self.assertEqual(after, before)
                    self.assertTrue(evalset.load_set(set_root, known_courts).ok)


class SharedManifest(unittest.TestCase):
    """One file hold allows appends and refuses changed prefixes."""

    def test_discard_vocabulary_and_key_order(self) -> None:
        self.assertEqual(manifest.DISCARD_KEYS, ("reason", "source_id", "edge", "kind"))
        self.assertEqual(set(manifest.DISCARD_REASONS), set(synthetic.DISCARD_REASONS) | {
            "request-failed", "http-status", "truncated", "off-schema", "closed-book-failed",
            "hash-changed", "not-ready", "already-cased", "document-short", "unpublished",
            "no-case-name", "cited-absent", "state-outranked", "no-state-kind", "same-document",
            "same-cluster", "no-kind", "premise-in-record",
        })
        self.assertEqual(len(manifest.DISCARD_REASONS), len(set(manifest.DISCARD_REASONS)))

    def test_manifest_format_key_order_and_sheet_suffixes(self) -> None:
        self.assertEqual(manifest.MANIFEST_FORMAT, 3)
        self.assertEqual(manifest.MANIFEST_KEYS, (
            "format", "set_root", "set_digest", "category", "case_pin", "template",
            "template_digest", "reading", "reading_digest", "generation", "seed", "asked",
            "read_share", "read_ordinals", "entries", "discards", "sheet_sha256", "sheet_bytes",
        ))
        self.assertIn("document-short", manifest.DISCARD_REASONS)
        self.assertIn("no-case-name", manifest.DISCARD_REASONS)
        self.assertEqual((manifest.SHEET_REQUIRED, manifest.SHEET_OPTIONAL), ("required", "optional"))

    def test_marks_template_renders_required_ordinals_and_a_header_to_complete(self) -> None:
        digest = hashlib.sha256(b"Fictitious sheet bytes").hexdigest()
        header = f"synthetic: <CSA role id> {digest[:manifest.DIGEST_PREFIX_CHARS]} <ISO date>\n"
        for ordinals, lines in (((), ""), ((1,), "01 \n"), ((1, 6, 101), "01 \n06 \n101 \n")):
            with self.subTest(ordinals=ordinals):
                text = manifest.marks_template(digest, ordinals)
                self.assertEqual(text, header + lines)
                self.assertIsNone(manifest.MARKS_HEADER_PATTERN.fullmatch(text.splitlines()[0]))
                completed = text.splitlines()[0].replace("<CSA role id>", "CSA-1").replace("<ISO date>", "2099-01-04")
                self.assertIsNotNone(manifest.MARKS_HEADER_PATTERN.fullmatch(completed))

    def test_absent_existing_appended_and_damaged_file_holds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            self.assertIsNone(manifest.pin_file(path))
            self.assertTrue(manifest.hold_file(path, None))
            original = b"Fictitious first line\nFictitious second line\n"
            path.write_bytes(original)
            pin = manifest.pin_file(path)
            self.assertEqual(pin, (2, hashlib.sha256(original).hexdigest()))
            self.assertTrue(manifest.hold_file(path, None))
            for raw, held in ((original, True), (original + b"Fictitious appended\n", True),
                              (original.replace(b"first", b"edited"), False), (original.splitlines(keepends=True)[0], False),
                              (original[:-1], False), (original + b"Partial", False)):
                path.write_bytes(raw)
                self.assertEqual(manifest.hold_file(path, pin), held)
            path.unlink()
            self.assertFalse(manifest.hold_file(path, pin))

    def test_original_link_matches_the_segmenter_sheet_rule(self) -> None:
        self.assertEqual(manifest.original_link(["1959 U.S. App. LEXIS 4083", "265 F.2d 521"]),
                         "https://www.courtlistener.com/c/F.2d/265/521/")
        self.assertEqual(manifest.original_link([]), "no printed reporter cite recorded; search the opinion id")

    def test_imports_at_every_depth_and_write_helpers_are_imported(self) -> None:
        for module in (draft, intake, manifest):
            assert module.__file__ is not None
            tree = ast.parse(Path(module.__file__).read_text())
            for node in ast.walk(tree):
                names: Sequence[str] = ()
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                    self.assertEqual(node.level, 0)
                for name in names:
                    self.assertTrue(name.split(".")[0] in sys.stdlib_module_names or name.startswith(("gideon.", "tools.segmenter", "tools.synthetic")))
                    if name.startswith("gideon.worker"):
                        self.assertIn(name, {"gideon.worker", "gideon.worker.citations"})
        for name in ("owner_of", "make_directory", "write_atomic"):
            self.assertIs(getattr(draft, name), getattr(segmenter_manifest, name))


if __name__ == "__main__":
    unittest.main()
