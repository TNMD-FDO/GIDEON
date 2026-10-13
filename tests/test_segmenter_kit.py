"""The segmenter draw and checked-sheet intake over fictitious files."""

import ast
import csv
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from gideon.evaluation import evalset, record, segmenter_draft, segmenter_labels
from gideon.host import backuplock, cas, engine, nogpu, site
from gideon.host.report import Problem
from gideon.host.sysio import PathLike
from gideon.worker import opiniontext, staging
from tools.segmenter import draft, draw, intake, manifest


def _csv(path: Path, header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, **draw.CSV_OPTIONS)  # type: ignore[arg-type]
        writer.writerow(header)
        writer.writerows(rows)


def _stage(root: Path, *, large: bool = False, no_text: bool = False, orphan: bool = False) -> Path:
    stage_root = root / "stage"
    counts: dict[str, dict[str, int]] = {}
    for court in draw.REQUIRED_COURTS:
        court_path = stage_root / court
        _csv(court_path / "opinion-clusters.csv", ("id", "date_filed"), [
            ("1" if court == "ca6" else "2", "2005-03-04"),
            ("3" if court == "ca6" else "4", "2015-04-05"),
        ])
        first_id = 101 if court == "ca6" else 201
        large_text = "A" * 200_000 + ' "quoted"' if large else "Fictitious text"
        rows: list[tuple[str, ...]] = []
        for offset in range(8):
            source = "plain_text" if offset == 0 else "html"
            fields = dict.fromkeys(draw.SOURCE_COLUMNS, "")
            if not (no_text and offset == 7):
                fields[source] = large_text if offset == 1 else f"Fictitious {offset}"
            cluster = "1" if court == "ca6" else "2"
            if offset == 0:
                cluster = "3" if court == "ca6" else "4"
            if orphan and offset == 6:
                cluster = "99"
            rows.append((str(first_id + offset), cluster, *(fields[name] for name in draw.SOURCE_COLUMNS)))
        _csv(court_path / "opinions.csv", ("id", "cluster_id", *draw.SOURCE_COLUMNS), rows)
        counts[court] = {"opinions": len(rows)}
    stage_root.mkdir(parents=True, exist_ok=True)
    (stage_root / "stage.json").write_text(json.dumps({
        "label": "corpus-2026-10-07", "snapshot": "caselaw-2026-09-30",
        "courts": list(draw.REQUIRED_COURTS), "counts": counts,
    }), encoding="utf-8")
    return stage_root


def _run_draw(args: list[str], root: Path) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    code = draw.main(args, root=root, stdout=output, stderr=errors)
    return code, output.getvalue(), errors.getvalue()


class Draw(unittest.TestCase):
    """The sample is a deterministic, content-free draw of staged rows."""

    def test_reader_dialect_limit_and_source_choice_match_worker(self) -> None:
        self.assertEqual(draw.CSV_OPTIONS, {
            "delimiter": ",", "quotechar": '"', "escapechar": "\\",
            "doublequote": False, "strict": True,
        })
        self.assertEqual(draw.CSV_FIELD_LIMIT, sys.maxsize)
        self.assertEqual(csv.field_size_limit(), sys.maxsize)
        self.assertEqual(draw.SOURCE_COLUMNS, opiniontext.TEXT_SOURCES)
        self.assertIn("csv.field_size_limit(sys.maxsize)", Path(staging.__file__).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = _stage(root, large=True)
            code, output, errors = _run_draw([str(stage), "--seed", "17", "--per-court", "3", "--out", str(root / "sample.jsonl")], root / "checkout")
            self.assertEqual((code, errors), (0, ""), output)
            sample_lines = [json.loads(line) for line in (root / "sample.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(sample_lines), 7)
            row = next(row for row in draw._read_csv(stage / "ca6" / "opinions.csv", ("id", "cluster_id", *draw.SOURCE_COLUMNS)) if row["id"] == "102")
            worker_rows = staging.read_rows(
                stage / "ca6" / "opinions.csv", "opinions", lambda path, mode: path.open(mode),
            )
            next(worker_rows)
            next(worker_rows)
            worker_fields, _ = next(worker_rows)
            worker_rows.close()
            self.assertEqual(worker_fields[-1], row["plain_text"])
            self.assertEqual(worker_fields[-2], row["html"])
            chosen = opiniontext.choose(row)
            assert chosen is not None
            self.assertEqual(next(name for name in draw.SOURCE_COLUMNS if row[name].strip()), chosen[0])
            self.assertIn('"quoted"', row["html"])
            self.assertNotIn("Fictitious", output)

    def test_no_text_is_counted_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = _stage(root, no_text=True)
            out = root / "sample.jsonl"
            code, output, errors = _run_draw(
                [str(stage), "--seed", "17", "--per-court", "3", "--out", str(out)],
                root / "checkout",
            )
            self.assertEqual((code, errors), (0, ""), output)
            header = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(header["skipped_no_text"], dict.fromkeys(draw.REQUIRED_COURTS, 1))
            self.assertIn("no text: 2", output)

    def test_opinion_without_a_cluster_is_counted_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = _stage(root, orphan=True)
            out = root / "sample.jsonl"
            code, output, errors = _run_draw(
                [str(stage), "--seed", "17", "--per-court", "3", "--out", str(out)],
                root / "checkout",
            )
            self.assertEqual((code, errors), (0, ""), output)
            lines = out.read_text(encoding="utf-8").splitlines()
            header = json.loads(lines[0])
            self.assertEqual(header["skipped_no_cluster"], dict.fromkeys(draw.REQUIRED_COURTS, 1))
            self.assertNotIn(107, [json.loads(line)["opinion_id"] for line in lines[1:]])
            self.assertIn("no cluster: 2", output)

    def test_thin_source_shortfall_spreads_by_population(self) -> None:
        # Equal shares 50/50/50; plain_text holds 1 row, so 49 seats go to
        # html and xml_harvard as 100:900, i.e. 4.9 and 44.1: 5 and 44 more.
        cells: dict[draw.Cell, list[int]] = {
            ("ca6", "plain_text", 2000): [1],
            ("ca6", "html", 2000): list(range(100, 200)),
            ("ca6", "xml_harvard", 2000): list(range(1000, 1900)),
            ("scotus", "html", 2000): list(range(5000, 5200)),
        }
        allocation = draw.allocate(cells, 150)
        self.assertEqual(
            [allocation[("ca6", source, 2000)] for source in ("plain_text", "html", "xml_harvard")],
            [1, 55, 94],
        )

    def test_sample_is_handed_to_the_output_owner_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = _stage(root)
            out = root / "made" / "sample.jsonl"
            calls: list[tuple[int, int]] = []
            real_fchown = manifest.os.fchown

            def record_fchown(descriptor: int, uid: int, gid: int) -> None:
                calls.append((uid, gid))
                real_fchown(descriptor, uid, gid)

            with patch.object(manifest.os, "fchown", side_effect=record_fchown):
                code, output, errors = _run_draw(
                    [str(stage), "--seed", "17", "--per-court", "3", "--out", str(out)],
                    root / "checkout",
                )
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(calls, [manifest.owner_of(root)])
            self.assertEqual(out.stat().st_mode & 0o777, 0o644)

    def test_replacement_keeps_the_mode_it_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.jsonl"
            path.write_bytes(b"old\n")
            path.chmod(0o664)
            manifest.write_atomic(path, b"new\n")
            self.assertEqual((path.read_bytes(), path.stat().st_mode & 0o777), (b"new\n", 0o664))

    def test_allocation_thin_source_and_one_row_decade(self) -> None:
        cells: dict[draw.Cell, list[int]] = {
            ("ca6", "plain_text", 1990): [1],
            ("ca6", "html", 2000): list(range(2, 12)),
            ("ca6", "html", 2010): [12],
            ("scotus", "html", 2000): list(range(20, 32)),
        }
        allocation = draw.allocate(cells, 6)
        self.assertEqual(allocation[("ca6", "plain_text", 1990)], 1)
        self.assertEqual(allocation[("ca6", "html", 2010)], 1)
        self.assertEqual(allocation[("ca6", "html", 2000)], 4)
        self.assertEqual(allocation[("scotus", "html", 2000)], 6)
        self.assertEqual(sum(count for (court, _, _), count in allocation.items() if court == "ca6"), 6)

    def test_determinism_write_and_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = _stage(root)
            paths = [root / f"sample-{index}.jsonl" for index in range(3)]
            for index, seed in enumerate((17, 17, 18)):
                code, _output, errors = _run_draw([str(stage), "--seed", str(seed), "--per-court", "3", "--out", str(paths[index])], root / "checkout")
                self.assertEqual((code, errors), (0, ""))
            self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
            self.assertNotEqual(paths[0].read_bytes(), paths[2].read_bytes())
            self.assertEqual(json.loads(paths[0].read_text(encoding="utf-8").splitlines()[0])["seed"], 17)
            set_root = root / "eval-v9"
            code, output, errors = _run_draw(["--write", str(paths[0]), "--reviewer", "CSA-1", "--on", "2026-10-08", "--set", str(set_root)], root / "checkout")
            self.assertEqual((code, errors), (0, ""), output)
            case_bytes = (set_root / draw.CASE_PATH).read_bytes()
            id_lines = (set_root / draw.IDS_PATH).read_text(encoding="utf-8").splitlines()
            cases = [json.loads(line) for line in case_bytes.decode().splitlines()]
            self.assertEqual(len(cases), 6)
            self.assertEqual([case["id"] for case in cases], id_lines)
            self.assertEqual(cases[0]["review"], {"by": "CSA-1", "on": "2026-10-08"})
            self.assertIn(hashlib.sha256(case_bytes).hexdigest(), output)
            self.assertIn(hashlib.sha256((set_root / draw.IDS_PATH).read_bytes()).hexdigest(), output)
            loaded = evalset.load_set(set_root)
            self.assertTrue(loaded.ok, loaded.findings)
            assert loaded.loaded is not None
            self.assertEqual(len(loaded.loaded.unsigned_ids), len(cases))
            code, _output, errors = _run_draw(["--write", str(paths[0]), "--reviewer", "CSA-1", "--on", "2026-10-08", "--set", str(set_root)], root / "checkout")
            self.assertEqual(code, 1)
            self.assertIn("Fix:", errors)
            self.assertEqual((set_root / draw.CASE_PATH).read_bytes(), case_bytes)
            code, _output, errors = _run_draw([str(stage), "--seed", "17", "--out", str(root / "checkout" / "sample.jsonl")], root / "checkout")
            self.assertEqual(code, 1)
            self.assertIn("outside the checkout", errors)

            failed_root = root / "eval-v10"
            write_file = manifest.write_atomic

            def fail_id_list(path: Path, payload: bytes) -> None:
                if path.name == "sample.ids":
                    raise OSError("fictitious write failure")
                write_file(path, payload)

            with patch.object(manifest, "write_atomic", side_effect=fail_id_list):
                code, _output, errors = _run_draw(
                    ["--write", str(paths[0]), "--reviewer", "CSA-1", "--on", "2026-10-08", "--set", str(failed_root)],
                    root / "checkout",
                )
            self.assertEqual(code, 1)
            self.assertIn("Fix:", errors)
            self.assertFalse((failed_root / draw.CASE_PATH).exists())
            self.assertFalse((failed_root / draw.IDS_PATH).exists())


def _case(case_id: str, opinion_id: int) -> dict[str, object]:
    return {
        "id": case_id, "suite": "build-gates", "category": "segmenter-300", "branch": "legal",
        "opinion_id": opinion_id, "court": "ca6", "decade": 2000, "text_source": "plain_text",
        "labels": ["drawn"], "cluster_id": case_id,
        "review": {"by": "CSA-1", "on": "2026-10-08"}, "notes": "",
    }


def _intake_fixture(root: Path) -> tuple[Path, Path, Path, str]:
    set_root = root / "eval-v9"
    case_path = set_root / draw.CASE_PATH
    case_path.parent.mkdir(parents=True)
    case_path.write_text("".join(json.dumps(_case(f"segmenter-{i:03d}", 100 + i)) + "\n" for i in (1, 2)), encoding="utf-8")
    ids = set_root / draw.IDS_PATH
    ids.parent.mkdir(parents=True)
    ids.write_text("segmenter-001\nsegmenter-002\n", encoding="utf-8")
    loaded = evalset.load_set(set_root)
    assert loaded.loaded is not None, loaded.findings
    text = "\n\n  Fictitious majority\n\nFictitious dissent"
    table = segmenter_draft.paragraph_table(text)
    sheets = root / "sheets"
    sheets.mkdir(parents=True)
    cases: list[dict[str, object]] = []
    for i in (1, 2):
        case_id = f"segmenter-{i:03d}"
        sheet = sheets / f"{case_id}.txt"
        sha256 = hashlib.sha256(text.encode()).hexdigest()
        sheet.write_text(
            manifest.sheet_header(case_id, 100 + i, "ca6", sha256, len(text), len(table.starts), "drafted")
            + f"\n{manifest.SHEET_PARAGRAPHS_HEADER}\n"
            "0000 [majority] Fictitious majority\n0001 [dissent] Fictitious dissent\n",
            encoding="utf-8",
        )
        cases.append(dict(zip(manifest.CASE_KEYS, (
            case_id, 100 + i, "b" * 64, sha256,
            len(text), list(table.starts), "drafted", None, hashlib.sha256(sheet.read_bytes()).hexdigest(),
        ), strict=True)))
    value = dict(zip(manifest.MANIFEST_KEYS, (
        manifest.MANIFEST_FORMAT, str(set_root.resolve()), loaded.loaded.digest,
        "segmenter-label@1", segmenter_draft.PROMPT_DIGESTS["segmenter-label@1"], cases,
    ), strict=True))
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return set_root, sheets, manifest_path, text


def _run_intake(manifest_path: Path, sheets: Path, *, set_root: Path, dry_run: bool = False) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    args = [str(manifest_path), str(sheets), "--signer", "CSA-1", "--on", "2026-10-08", "--set", str(set_root)]
    if dry_run:
        args.append("--dry-run")
    code = intake.main(args, stdout=output, stderr=errors)
    return code, output.getvalue(), errors.getvalue()


class Intake(unittest.TestCase):
    """All sheets validate before labels are appended, and edits to text are ignored."""

    def test_dry_run_append_pin_first_span_and_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, sheets, manifest_path, text = _intake_fixture(Path(directory))
            label_path = segmenter_labels.path_for(set_root)
            sheet = sheets / "segmenter-001.txt"
            sheet.write_text(sheet.read_text(encoding="utf-8").replace("Fictitious dissent", "damaged editor text"), encoding="utf-8")
            code, output, errors = _run_intake(manifest_path, sheets, set_root=set_root, dry_run=True)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertFalse(label_path.exists())
            self.assertIn("(dry-run)", output)
            code, output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
            self.assertEqual((code, errors), (0, ""), output)
            payload = label_path.read_bytes()
            self.assertIn(hashlib.sha256(payload).hexdigest(), output)
            parsed = segmenter_labels.parse_bytes(payload)
            self.assertFalse(parsed.findings)
            self.assertEqual(len(parsed.records), 2)
            self.assertEqual(parsed.records[0].record.sections[0].start, 0)
            self.assertGreater(segmenter_draft.paragraph_table(text).starts[0], 0)
            self.assertEqual(parsed.records[0].record.sections[-1].end, len(text))
            code, _output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
            self.assertEqual(code, 1)
            self.assertIn("segmenter-001", errors)
            self.assertEqual(label_path.read_bytes(), payload)

    def test_spaced_mark_bare_number_and_blank_lines_are_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, sheets, manifest_path, _text = _intake_fixture(Path(directory))
            sheet = sheets / "segmenter-001.txt"
            sheet.write_text(
                sheet.read_text(encoding="utf-8")
                .replace("[majority]", "[ majority ]")
                .replace("0001 [dissent] Fictitious dissent", "0001")
                + "\n\n",
                encoding="utf-8",
            )
            code, output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
            self.assertEqual((code, errors), (0, ""), output)
            first = segmenter_labels.parse_bytes(segmenter_labels.path_for(set_root).read_bytes())
            self.assertEqual(
                [span.section_type for span in first.records[0].record.sections], ["majority"]
            )

    def test_sheet_refusals_are_located_and_atomic(self) -> None:
        replacements = (
            ("0001 [dissent]", "0000 [dissent]", "appears twice"),
            ("0001 [dissent]", "0002 [dissent]", "outside the table"),
            ("0001 [dissent]", "0001 [not_a_type]", "unknown"),
            ("0000 [majority]", "0000", "first paragraph"),
            ("0000 [majority]", "0001 [majority]", "out of order"),
        )
        for before, after, rule in replacements:
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as directory:
                set_root, sheets, manifest_path, _text = _intake_fixture(Path(directory))
                sheet = sheets / "segmenter-002.txt"
                sheet.write_text(sheet.read_text(encoding="utf-8").replace(before, after), encoding="utf-8")
                code, _output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
                self.assertEqual(code, 1)
                self.assertIn("case segmenter-002 line", errors)
                self.assertIn(rule, errors)
                self.assertIn("Fix:", errors)
                self.assertFalse(segmenter_labels.path_for(set_root).exists())

    def test_wrong_sheet_header_and_failed_replace_write_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, sheets, manifest_path, _text = _intake_fixture(Path(directory))
            sheet = sheets / "segmenter-002.txt"
            sheet.write_text(sheet.read_text(encoding="utf-8").replace("sha256 ", "sha256 bad-", 1), encoding="utf-8")
            code, _output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
            self.assertEqual(code, 1)
            self.assertIn("case segmenter-002 line 1", errors)
            self.assertFalse(segmenter_labels.path_for(set_root).exists())
            _set, sheets, manifest_path, _text = _intake_fixture(Path(directory) / "fresh")
            with patch.object(manifest.os, "replace", side_effect=OSError):
                code, _output, errors = _run_intake(manifest_path, sheets, set_root=_set)
            self.assertEqual(code, 1)
            self.assertIn("Fix:", errors)
            self.assertFalse(segmenter_labels.path_for(_set).exists())

    def test_manifest_and_set_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            set_root, sheets, manifest_path, _text = _intake_fixture(Path(directory))
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            value["cases"][0]["starts"] = [0, 0]
            manifest_path.write_text(json.dumps(value), encoding="utf-8")
            code, _output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
            self.assertEqual(code, 1)
            self.assertIn("manifest", errors)
            self.assertFalse(segmenter_labels.path_for(set_root).exists())
            _set, _sheets, manifest_path, _text = _intake_fixture(Path(directory) / "fresh")
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            value["set_digest"] = "a" * 64
            manifest_path.write_text(json.dumps(value), encoding="utf-8")
            code, _output, errors = _run_intake(manifest_path, _sheets, set_root=_set)
            self.assertEqual(code, 1)
            self.assertIn("changed since draft", errors)


class DraftHost:
    """A content-free record, store, and engine-lock host for the draft command."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.documents: list[dict[str, object]] = [
            {"opinion_id": opinion_id, "doc_id": "b" * 64,
             "text_source": "plain_text", "canonical_sha256": self.sha256,
             "reporter_cites": cites}
            for opinion_id, cites in ((101, ["1959 U.S. App. LEXIS 4083", "265 F.2d 521"]), (102, None))
        ]
        self.record_rc = 0
        self.record_output: str | None = None
        self.objects: dict[str, bytes] = {self.sha256: text.encode("utf-8")}
        self.no_gpu = False
        self.root = True
        self.holder: str | None = None
        self.claimed: list[tuple[PathLike, str]] = []
        self.released: list[PathLike] = []
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def geteuid(self) -> int:
        return 0 if self.root else 1000

    def exists(self, path: PathLike) -> bool:
        return Path(path) == nogpu.NO_GPU_PATH and self.no_gpu

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False,
              exist_ok: bool = False) -> None:
        del path, mode, parents, exist_ok

    def take_lock(self, path: PathLike, record_text: str) -> str | None:
        self.claimed.append((path, record_text))
        return self.holder

    def release_lock(self, path: PathLike) -> None:
        self.released.append(path)

    def run(
        self, argv: tuple[str, ...] | list[str], *, check: bool = False,
        input: str | None = None, cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout, passthrough
        self.calls.append((tuple(argv), input))
        requested = json.loads(input.split("'", 2)[1]) if input is not None else []
        return subprocess.CompletedProcess(
            list(argv), self.record_rc,
            self.record_output if self.record_output is not None else json.dumps([
                row for row in self.documents if row["opinion_id"] in requested
            ]),
            "private record diagnostic",
        )

    def stat(self, path: PathLike) -> os.stat_result:
        if Path(path) != cas.ROOT:
            raise FileNotFoundError(path)
        return os.stat_result((stat.S_IFDIR | 0o770, 0, 0, 0, 0, 0, 0, 0, 0, 0))

    def read_bytes(self, path: PathLike) -> bytes:
        name = Path(path).name
        if name not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[name]


def _engine_reply(*, finish: str = "stop") -> engine.EngineReply:
    content = json.dumps({"sections": [
        {"paragraph": 0, "type": "majority"},
        {"paragraph": 1, "type": "dissent"},
    ]})
    value = {"choices": [{"finish_reason": finish, "message": {"content": content}}],
             "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
    return engine.EngineReply(200, json.dumps(value), value, (), True, None, 1.5, None)


def _run_draft(host: DraftHost, set_root: Path, out: Path, *, cases: tuple[str, ...] = (),
               target: engine.EngineTarget | Problem | None = None,
               finish: str = "stop", site_state: site.SiteLoadResult | None = None
               ) -> tuple[int, str, str, list[Mapping[str, object]]]:
    output, errors = io.StringIO(), io.StringIO()
    bodies: list[Mapping[str, object]] = []

    def call(*_args: object, **kwargs: object) -> engine.EngineReply:
        body = kwargs["body"]
        assert isinstance(body, Mapping)
        bodies.append(body)
        return _engine_reply(finish=finish)

    config = cast(site.SiteConfig, SimpleNamespace(hardware_profile="fixture-profile"))
    selected_target = target if target is not None else engine.EngineTarget("fixture-profile", "fixture-model", 262_144)
    args = ["--out", str(out), "--set", str(set_root)]
    for case_id in cases:
        args.extend(("--case", case_id))
    with (
        patch.object(site, "load_site", return_value=site_state if site_state is not None else site.SiteLoadResult(config=config)),
        patch.object(engine, "resolve_engine_target", return_value=selected_target),
        patch.object(engine, "call_model_server", side_effect=call),
    ):
        code = draft.main(
            args, host=cast(draft.DraftHost, host), root=set_root.parent / "checkout",
            rendered_dir="/tmp/fictitious-render", site_path="/etc/gideon/site.yaml",
            models_path=set_root.parent / "checkout/models.lock",
            clock=lambda: datetime(2026, 10, 8, tzinfo=UTC), sleep=lambda _seconds: None,
            stdout=output, stderr=errors,
        )
    return code, output.getvalue(), errors.getvalue(), bodies


class DraftTool(unittest.TestCase):
    """The on-box draft uses one lock, read, store, and fake generator."""

    def test_round_trip_to_checked_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, _unused_sheets, _unused_manifest, text = _intake_fixture(root)
            host = DraftHost(text)
            out = root / "drafted"
            code, output, errors, bodies = _run_draft(host, set_root, out)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(len(bodies), 2)
            self.assertTrue(all(body["model"] == "fixture-model" for body in bodies))
            self.assertEqual(host.claimed[0][0], backuplock.ENGINE_LOCK.path)
            self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
            self.assertEqual(len(host.calls), 1)
            argv, sql = host.calls[0]
            self.assertEqual(list(argv), record.reader_argv("/tmp/fictitious-render"))
            assert sql is not None
            self.assertEqual(sql.count("\\set opinion_ids "), 1)
            self.assertIn("jsonb_array_elements_text(:'opinion_ids'::jsonb)", sql)
            self.assertNotIn(text, sql)
            value = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(tuple(value), manifest.MANIFEST_KEYS)
            self.assertEqual(value["set_root"], str(set_root.resolve()))
            self.assertEqual(value["prompt_digest"], segmenter_draft.PROMPT_DIGESTS[value["prompt"]])
            self.assertEqual(len(value["cases"]), 2)
            table = segmenter_draft.paragraph_table(text)
            for entry in value["cases"]:
                self.assertEqual(tuple(entry), manifest.CASE_KEYS)
                self.assertEqual(entry["starts"], list(table.starts))
                self.assertEqual(entry["status"], "drafted")
                sheet = (out / f"{entry['case_id']}.txt").read_text(encoding="utf-8")
                self.assertEqual(hashlib.sha256(sheet.encode()).hexdigest(), entry["sheet_sha256"])
                self.assertEqual(sheet.splitlines()[0], manifest.sheet_header(
                    entry["case_id"], entry["opinion_id"], "ca6", entry["sha256"],
                    len(text), len(table.starts), "drafted",
                ))
                self.assertIn("0000 [majority]", sheet)
                self.assertIn("0001 [dissent]", sheet)
                if entry["opinion_id"] == 101:
                    self.assertIn("Cited as: 1959 U.S. App. LEXIS 4083; 265 F.2d 521\n", sheet)
                    self.assertIn("Original: https://www.courtlistener.com/c/F.2d/265/521/\n", sheet)
                else:
                    self.assertIn("Cited as: no cite recorded\n", sheet)
                    self.assertIn("Original: no printed reporter cite recorded", sheet)
                self.assertIn("Fictitious dissent", (out / f"{entry['case_id']}.full.txt").read_text(encoding="utf-8"))
            self.assertIn("summary: drafted 2", output)
            self.assertNotIn(text, output + errors)
            intake_code, intake_output, intake_errors = _run_intake(out / "manifest.json", out, set_root=set_root)
            self.assertEqual((intake_code, intake_errors), (0, ""), intake_output)
            parsed = segmenter_labels.parse_bytes(segmenter_labels.path_for(set_root).read_bytes())
            self.assertFalse(parsed.findings)
            self.assertEqual(len(parsed.records), 2)
            loaded = evalset.load_set(set_root)
            self.assertTrue(loaded.ok, loaded.findings)
            assert loaded.loaded is not None
            self.assertFalse(loaded.loaded.unsigned_ids)
            again = DraftHost(text)
            code, _output, errors, _bodies = _run_draft(again, set_root, root / "nothing-unsigned")
            self.assertEqual(code, 1)
            self.assertIn("no unsigned segmenter case", errors)
            self.assertEqual(again.claimed, [])

    def test_missing_document_skips_and_case_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, _sheets, _manifest, text = _intake_fixture(root)
            host = DraftHost(text)
            host.documents = host.documents[:1]
            code, output, errors, _bodies = _run_draft(host, set_root, root / "skipped")
            self.assertEqual((code, errors), (0, ""), output)
            self.assertIn("segmenter-002: skipped not-ingested", output)
            value = json.loads((root / "skipped/manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([item["case_id"] for item in value["cases"]], ["segmenter-001"])
            host = DraftHost(text)
            code, _output, errors, bodies = _run_draft(host, set_root, root / "filtered", cases=("segmenter-002",))
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(len(bodies), 1)
            self.assertEqual([item["case_id"] for item in json.loads((root / "filtered/manifest.json").read_text())["cases"]], ["segmenter-002"])

    def test_failed_draft_marks_unknown_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, _sheets, _manifest, text = _intake_fixture(root)
            host = DraftHost(text)
            code, output, errors, _bodies = _run_draft(host, set_root, root / "failed", finish="length")
            self.assertEqual((code, errors), (0, ""), output)
            value = json.loads((root / "failed/manifest.json").read_text())
            self.assertEqual([item["failure_code"] for item in value["cases"]], ["truncated", "truncated"])
            sheet = (root / "failed/segmenter-001.txt").read_text()
            self.assertIn("0000 [unknown]", sheet)
            self.assertIn("0001 - ", sheet)
            self.assertEqual(sheet.count("[unknown]"), 1)
            self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
            self.assertIn("failed truncated", output)
            self.assertNotIn(text, output + errors)

    def test_refusals_keep_fixes_and_lock_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, _sheets, _manifest, text = _intake_fixture(root)
            scenarios = ("root", "holder", "no-gpu", "site", "engine", "read-exit", "read-json", "missing-object", "corrupt-object")
            for scenario in scenarios:
                with self.subTest(scenario=scenario):
                    host = DraftHost(text)
                    target: engine.EngineTarget | Problem | None = None
                    site_state: site.SiteLoadResult | None = None
                    if scenario == "root":
                        host.root = False
                    elif scenario == "holder":
                        host.holder = backuplock.Record("other command", 999999, datetime(2026, 10, 8, tzinfo=UTC)).to_json()
                    elif scenario == "no-gpu":
                        host.no_gpu = True
                    elif scenario == "site":
                        site_state = site.SiteLoadResult(errors=(site.SiteError(None, "fictitious site error", "Repair the site file."),))
                    elif scenario == "engine":
                        target = Problem("generator unavailable", "Run engine verify, then retry.")
                    elif scenario == "read-exit":
                        host.record_rc = 2
                    elif scenario == "read-json":
                        host.record_output = "private invalid JSON"
                    elif scenario == "missing-object":
                        host.objects.clear()
                    else:
                        host.objects[host.sha256] = b"corrupt"
                    code, output, errors, _bodies = _run_draft(host, set_root, root / scenario, target=target, site_state=site_state)
                    self.assertEqual(code, 1, output)
                    self.assertIn("Fix:", errors)
                    self.assertNotIn(text, output + errors)
                    self.assertEqual(host.released, [] if scenario in {"root", "holder"} else [backuplock.ENGINE_LOCK.path])
                    self.assertFalse((root / scenario / "manifest.json").exists())
                    if scenario == "holder":
                        self.assertIn("other command", errors)
            host = DraftHost(text)
            (root / "nonempty").mkdir()
            (root / "nonempty" / "file").write_text("fictitious")
            for out in (root / "nonempty", set_root.parent / "checkout" / "inside"):
                code, _output, errors, _bodies = _run_draft(host, set_root, out)
                self.assertEqual(code, 1)
                self.assertIn("empty output directory outside the checkout", errors)
            self.assertEqual(host.claimed, [])

            host = DraftHost(text)
            with patch.object(manifest, "write_atomic", side_effect=OSError("fictitious write error")):
                code, _output, errors, _bodies = _run_draft(host, set_root, root / "write-error")
            self.assertEqual(code, 1)
            self.assertIn("Fix:", errors)
            self.assertEqual(host.released, [backuplock.ENGINE_LOCK.path])
            self.assertFalse((root / "write-error/manifest.json").exists())


class Imports(unittest.TestCase):
    """Kit modules keep worker dependencies outside their runtime imports."""

    def test_module_imports(self) -> None:
        for module in (draft, draw, intake, manifest):
            with self.subTest(module=module.__name__):
                assert module.__file__ is not None
                tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
                for node in tree.body:
                    if isinstance(node, ast.Import):
                        self.assertTrue(all(
                            name.name.split(".")[0] in sys.stdlib_module_names
                            or name.name.startswith(("gideon.", "tools.segmenter."))
                            for name in node.names
                        ))
                    elif isinstance(node, ast.ImportFrom):
                        self.assertTrue(
                            (node.module or "").split(".")[0] in sys.stdlib_module_names
                            or (node.module or "").startswith(("gideon.", "tools.segmenter"))
                        )
                        self.assertFalse((node.module or "").startswith("gideon.worker"))


if __name__ == "__main__":
    unittest.main()
