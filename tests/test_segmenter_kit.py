"""The segmenter draw, checked-sheet intake, and append-only file holds."""

import ast
import csv
import hashlib
import io
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
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
        "label": draw.FROZEN_LABEL, "snapshot": "caselaw-2026-09-30",
        "courts": list(draw.REQUIRED_COURTS), "counts": counts,
    }), encoding="utf-8")
    return stage_root


def _run_draw(args: list[str], root: Path) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    code = draw.main(args, root=root, stdout=output, stderr=errors)
    return code, output.getvalue(), errors.getvalue()


SUCCESSOR_TEXT = "Fictitious successor opinion sentinel."
SHADOW_TEXT = "Fictitious lower-priority source sentinel."
# A cipher both lacks function words and marks its digit runs with a capital.
CIPHER_TEXT = "qxzv 4K " * (opiniontext.LEGIBILITY_WORD_FLOOR + 1)
EMPTY_HTML = "<p></p>"


def _opinion_texts(stage: Path, replacements: Mapping[int, str]) -> None:
    for court in draw.REQUIRED_COURTS:
        path = stage / court / "opinions.csv"
        header = ("id", "cluster_id", *draw.SOURCE_COLUMNS)
        rows = list(draw._read_csv(path, header))
        for row in rows:
            if int(row["id"]) in replacements:
                row["html"] = replacements[int(row["id"])]
        _csv(path, header, [tuple(row[key] for key in header) for row in rows])


@dataclass(frozen=True)
class SuccessorFixture:
    """A fictitious stage and minimal set, with independently shuffled cell orders."""

    root: Path
    stage: Path
    set_root: Path
    plain_sample: Path
    seed: int
    per_court: int
    orders: Mapping[draw.Cell, tuple[int, ...]]
    allocation: Mapping[draw.Cell, int]

    def cases(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in (self.set_root / manifest.CASE_PATH).read_bytes().splitlines()]

    def case_ids(self, cell: draw.Cell) -> tuple[str, ...]:
        return tuple(cast(str, case["id"]) for case in self.cases()
                     if (case["court"], case["text_source"], case["decade"]) == cell)

    def candidates(self, cell: draw.Cell) -> tuple[int, ...]:
        used = {case["opinion_id"] for case in self.cases()}
        return tuple(opinion_id for opinion_id in self.orders[cell][self.allocation[cell]:] if opinion_id not in used)

    def arguments(self, out: Path, retired: Sequence[str], *, seed: int | None = None) -> list[str]:
        arguments = [str(self.stage), "--seed", str(self.seed if seed is None else seed),
                     "--per-court", str(self.per_court), "--out", str(out)]
        for case_id in retired:
            arguments.extend(("--retire", case_id))
        return arguments

    def write_arguments(self, sample: Path) -> list[str]:
        return ["--write", str(sample), "--reviewer", "CSA-1", "--on", "2001-02-03",
                "--set", str(self.set_root)]

    def append(self, opinion_id: int, cell: draw.Cell, *, supersedes: str | None = None,
               ordinal: int | None = None) -> str:
        cases = self.cases()
        if ordinal is None:
            ordinal = max(int(cast(str, case["id"]).split("-")[1]) for case in cases) + 1
        case_id = f"segmenter-{ordinal:03d}"
        case = _case(case_id, opinion_id)
        case.update(court=cell[0], text_source=cell[1], decade=cell[2])
        if supersedes is not None:
            case["supersedes"] = supersedes
        with (self.set_root / manifest.CASE_PATH).open("a", encoding="utf-8") as output:
            output.write(json.dumps(case) + "\n")
        for relative in (manifest.IDS_PATH, manifest.MIRROR_IDS_PATH):
            with (self.set_root / relative).open("a", encoding="utf-8") as output:
                output.write(case_id + "\n")
        result = evalset.load_set(self.set_root)
        assert result.ok, result.findings
        return case_id


def _successor_fixture(root: Path) -> SuccessorFixture:
    stage = root / "stage"
    seed, per_court = 17, 8
    populations: dict[draw.Cell, tuple[int, ...]] = {}
    counts: dict[str, dict[str, int]] = {}
    for ordinal, court in enumerate(draw.REQUIRED_COURTS, 1):
        clusters: list[tuple[str, ...]] = []
        opinions: list[tuple[str, ...]] = []
        next_id = ordinal * 10_000
        for decade, population in ((1990, 8), (2000, draw.CHECK_WINDOW * 2 + 16), (2010, 8), (2020, 1)):
            cluster = str(decade)
            clusters.append((cluster, f"{decade + 1}-01-01"))
            ids = tuple(range(next_id + 1, next_id + population + 1))
            populations[(court, "html", decade)] = ids
            next_id += population
            for opinion_id in ids:
                fields = dict.fromkeys(draw.SOURCE_COLUMNS, "")
                fields.update(html=f"<p>{SUCCESSOR_TEXT}</p>", plain_text=SHADOW_TEXT)
                opinions.append((str(opinion_id), cluster, *(fields[name] for name in draw.SOURCE_COLUMNS)))
        _csv(stage / court / "opinion-clusters.csv", ("id", "date_filed"), clusters)
        _csv(stage / court / "opinions.csv", ("id", "cluster_id", *draw.SOURCE_COLUMNS), opinions)
        counts[court] = {"opinions": len(opinions)}
    (stage / "stage.json").write_text(json.dumps({
        "label": draw.FROZEN_LABEL, "snapshot": "fictitious-successor-snapshot",
        "courts": list(draw.REQUIRED_COURTS), "counts": counts,
    }), encoding="utf-8")
    plain_sample = root / "draw.jsonl"
    checkout = root / "checkout"
    code, _output, errors = _run_draw([
        str(stage), "--seed", str(seed), "--per-court", str(per_court), "--out", str(plain_sample),
    ], checkout)
    assert code == 0, errors
    header, *samples = [json.loads(line) for line in plain_sample.read_bytes().splitlines()]
    allocation = {(row["court"], row["text_source"], row["decade"]): row["count"] for row in header["allocation"]}
    randomizer = random.Random(seed)
    orders = {}
    for cell in sorted(populations):
        shuffled = list(populations[cell])
        randomizer.shuffle(shuffled)
        orders[cell] = tuple(shuffled)
    set_root = checkout / evalset.SET_ROOT
    case_path = set_root / manifest.CASE_PATH
    case_path.parent.mkdir(parents=True)
    cases = []
    for ordinal, sample in enumerate(samples, 1):
        case = _case(f"segmenter-{ordinal:03d}", sample["opinion_id"])
        case.update({key: sample[key] for key in ("court", "decade", "text_source")})
        cases.append(case)
    case_path.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
    for relative in (manifest.IDS_PATH, manifest.MIRROR_IDS_PATH):
        path = set_root / relative
        path.parent.mkdir(parents=True)
        path.write_text("".join(f"{case['id']}\n" for case in cases), encoding="utf-8")
    result = evalset.load_set(set_root)
    assert result.ok, result.findings
    fixture = SuccessorFixture(root, stage, set_root, plain_sample, seed, per_court, orders, allocation)
    candidates = fixture.candidates(("ca6", "html", 2000))
    _opinion_texts(stage, {candidates[0]: CIPHER_TEXT, candidates[1]: EMPTY_HTML})
    return fixture


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

    def _assert_content_free(self, *values: str) -> None:
        for value in values:
            for text in (SUCCESSOR_TEXT, SHADOW_TEXT, "qxzv", EMPTY_HTML):
                self.assertNotIn(text, value)

    def _assert_refused(self, result: tuple[int, str, str], stage: str) -> None:
        code, output, errors = result
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertTrue(errors.startswith(f"{stage}: refused"), errors)
        self.assertIn("Fix:", errors)
        self._assert_content_free(output, errors)

    def _successor_sample(
        self, fixture: SuccessorFixture, retired: Sequence[str], *, name: str = "successors.jsonl",
    ) -> tuple[Path, dict, list[dict], str]:
        out = fixture.root / name
        code, output, errors = _run_draw(fixture.arguments(out, retired), fixture.root / "checkout")
        self.assertEqual((code, errors), (0, ""), output)
        self._assert_content_free(output, errors, out.read_text(encoding="utf-8"))
        header, *rows = [json.loads(line) for line in out.read_bytes().splitlines()]
        return out, header, rows, output

    def test_plain_draw_keeps_the_legacy_shuffled_prefix_bytes(self) -> None:
        """Successor support leaves the original draw's seeded bytes alone."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _successor_fixture(Path(directory))
            header = json.loads(fixture.plain_sample.read_bytes().splitlines()[0])
            expected_rows = [
                {"opinion_id": opinion_id, "court": court, "decade": decade, "text_source": source}
                for (court, source, decade), ids in sorted(fixture.orders.items())
                for opinion_id in sorted(ids[:fixture.allocation[(court, source, decade)]])
            ]
            expected = "".join(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                               for value in [header, *expected_rows]).encode("utf-8")
            self.assertEqual(fixture.plain_sample.read_bytes(), expected)

    def test_successor_sample_rank_checks_shape_ownership_and_content(self) -> None:
        """The ingest rejects ciphers and empty text without exposing either."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _successor_fixture(Path(directory))
            cell = ("ca6", "html", 2000)
            retired = fixture.case_ids(cell)[:2]
            candidates = fixture.candidates(cell)
            for raw, reason in ((CIPHER_TEXT, "illegible"), (EMPTY_HTML, "empty")):
                with self.subTest(reason=reason), self.assertRaises(opiniontext.OpinionTextFailure) as caught:
                    opiniontext.canonical_text("html", raw)
                self.assertEqual(caught.exception.reason, reason)
            with (
                patch.object(opiniontext, "canonical_text", wraps=opiniontext.canonical_text) as canonical,
                patch.object(opiniontext, "choose", wraps=opiniontext.choose) as choose,
                patch.object(manifest, "write_atomic", wraps=manifest.write_atomic) as write_sample,
            ):
                out, header, rows, output = self._successor_sample(fixture, tuple(reversed(retired)))
            expected_checked = dict.fromkeys(draw.REQUIRED_COURTS, 0)
            expected_checked[cell[0]] = min(draw.CHECK_WINDOW, len(candidates))
            stage = json.loads((fixture.stage / "stage.json").read_text(encoding="utf-8"))
            expected_header = {
                "seed": fixture.seed, "label": stage["label"], "snapshot": stage["snapshot"],
                "per_court": fixture.per_court, "mode": "successors", "retired": [
                    {"case_id": case_id, "court": cell[0], "text_source": cell[1], "decade": cell[2], "fallback": None}
                    for case_id in retired
                ], "checked": expected_checked,
            }
            expected_rows = [
                {"opinion_id": opinion_id, "court": cell[0], "decade": cell[2], "text_source": cell[1], "supersedes": case_id}
                for case_id, opinion_id in zip(retired, candidates[2:4], strict=True)
            ]
            self.assertEqual(header, expected_header)
            self.assertEqual(tuple(header), tuple(expected_header))
            self.assertEqual(rows, expected_rows)
            for item, expected_item in zip(header["retired"], expected_header["retired"], strict=True):
                self.assertEqual(tuple(item), tuple(expected_item))
            self.assertEqual(out.read_bytes(), "".join(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                for value in [expected_header, *expected_rows]
            ).encode("utf-8"))
            self.assertEqual(output.splitlines(), [
                *(f"{case_id}: ca6 html 2000; opinion: {opinion_id}; rank: {rank}; checked and failed: 2"
                  for rank, (case_id, opinion_id) in enumerate(zip(retired, candidates[2:4], strict=True), 3)),
                f"successors: {len(retired)}; checked: {sum(expected_checked.values())}",
            ])
            self.assertEqual(canonical.call_count, sum(expected_checked.values()))
            self.assertEqual(choose.call_count, canonical.call_count)
            for choice, check in zip(choose.call_args_list, canonical.call_args_list, strict=True):
                self.assertEqual(check.args, opiniontext.choose(choice.args[0]))
                self.assertEqual(check.args[0], cell[1])
                self.assertNotEqual(check.args[1], SHADOW_TEXT)
            write_sample.assert_called_once()
            owner = fixture.root.stat()
            self.assertEqual(write_sample.call_args.kwargs["owner"], (owner.st_uid, owner.st_gid))
            self.assertEqual((out.stat().st_uid, out.stat().st_gid), (owner.st_uid, owner.st_gid))

    def test_successors_check_rank_windows_once_per_needed_court_per_round(self) -> None:
        """CSV row order cannot choose a higher-ranked passing candidate."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _successor_fixture(Path(directory))
            cell = ("ca6", "html", 2000)
            candidates = fixture.candidates(cell)
            failed = draw.CHECK_WINDOW + 2
            _opinion_texts(fixture.stage, {
                opinion_id: CIPHER_TEXT if index % 2 else EMPTY_HTML
                for index, opinion_id in enumerate(candidates[:failed])
            })
            other_cell = ("ca6", "html", 1990)
            other_court = ("scotus", "html", 2000)
            retired = (*fixture.case_ids(cell)[:2], fixture.case_ids(other_cell)[0], fixture.case_ids(other_court)[0])
            passes: list[str] = []
            read_csv = draw._read_csv

            def read(path: Path, required: tuple[str, ...], *, stage: str = "stage") -> Iterator[dict[str, str]]:
                if stage == "check":
                    passes.append(path.parent.name)
                yield from read_csv(path, required, stage=stage)

            with (
                patch.object(draw, "_read_csv", side_effect=read),
                patch.object(opiniontext, "canonical_text", wraps=opiniontext.canonical_text) as canonical,
            ):
                _, header, rows, output = self._successor_sample(fixture, retired)
            by_retired = {row["supersedes"]: row for row in rows}
            for rank, case_id in enumerate(fixture.case_ids(cell)[:2], failed + 1):
                self.assertEqual(by_retired[case_id]["opinion_id"], candidates[rank - 1])
                self.assertIn(f"rank: {rank}; checked and failed: {failed}", output)
            self.assertEqual(by_retired[fixture.case_ids(other_cell)[0]]["opinion_id"], fixture.candidates(other_cell)[0])
            self.assertEqual(by_retired[fixture.case_ids(other_court)[0]]["opinion_id"], fixture.candidates(other_court)[0])
            rounds = (failed + 2 + draw.CHECK_WINDOW - 1) // draw.CHECK_WINDOW
            self.assertEqual(passes, ["ca6", "scotus"] + ["ca6"] * (rounds - 1))
            expected_checked = {
                "ca6": min(rounds * draw.CHECK_WINDOW, len(candidates)) + min(draw.CHECK_WINDOW, len(fixture.candidates(other_cell))),
                "scotus": min(draw.CHECK_WINDOW, len(fixture.candidates(other_court))),
            }
            self.assertEqual(header["checked"], expected_checked)
            self.assertEqual(canonical.call_count, sum(expected_checked.values()))

    def test_successor_skips_opinions_of_active_and_retired_cases(self) -> None:
        """An opinion already represented by any case cannot be redrawn."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _successor_fixture(Path(directory))
            cell = ("ca6", "html", 2000)
            candidates = fixture.candidates(cell)
            active_id = fixture.append(candidates[2], cell)
            retired_id = fixture.append(candidates[3], cell)
            fixture.append(candidates[4], cell, supersedes=retired_id)
            loaded = evalset.load_set(fixture.set_root).loaded
            assert loaded is not None
            self.assertIn(active_id, loaded.active_ids)
            self.assertNotIn(retired_id, loaded.active_ids)
            _, header, rows, output = self._successor_sample(fixture, fixture.case_ids(cell)[:1])
            self.assertEqual(rows[0]["opinion_id"], candidates[5])
            self.assertEqual(rows[0]["opinion_id"], fixture.candidates(cell)[2])
            self.assertIn("rank: 3; checked and failed: 2", output)
            self.assertEqual(header["checked"][cell[0]], min(draw.CHECK_WINDOW, len(fixture.candidates(cell))))

    def test_successor_replay_refuses_changed_stage_and_seed(self) -> None:
        """A different stage label, population, or seed cannot replace the draw."""

        for change in ("label", "population", "seed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                fixture = _successor_fixture(Path(directory))
                retired = fixture.case_ids(("ca6", "html", 2000))[:1]
                seed = fixture.seed
                if change == "label":
                    path = fixture.stage / "stage.json"
                    stage = json.loads(path.read_text(encoding="utf-8"))
                    stage["label"] = "fictitious-other-stage"
                    path.write_text(json.dumps(stage), encoding="utf-8")
                elif change == "population":
                    path = fixture.stage / "ca6" / "opinions.csv"
                    columns = ("id", "cluster_id", *draw.SOURCE_COLUMNS)
                    rows = list(draw._read_csv(path, columns))
                    original_id = next(case["opinion_id"] for case in fixture.cases() if case["id"] == retired[0])
                    new_id = max(opinion_id for ids in fixture.orders.values() for opinion_id in ids) + 1
                    for row in rows:
                        if int(row["id"]) == original_id:
                            row["id"] = str(new_id)
                    _csv(path, columns, [tuple(row[key] for key in columns) for row in rows])
                else:
                    seed += 1
                out = fixture.root / "refused.jsonl"
                result = _run_draw(fixture.arguments(out, retired, seed=seed), fixture.root / "checkout")
                self._assert_refused(result, "replay")
                self.assertIn(draw.FROZEN_LABEL, result[2])
                self.assertRegex(result[2], r"seed [0-9]+")
                self.assertFalse(out.exists())

    def test_successor_empty_cell_falls_back_to_nearest_decade(self) -> None:
        """A cell drawn whole falls back within its own court and source."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _successor_fixture(Path(directory))
            original, fallback = ("ca6", "html", 2020), ("ca6", "html", 2010)
            self.assertEqual(fixture.candidates(original), ())
            _, header, rows, output = self._successor_sample(fixture, fixture.case_ids(original))
            self.assertEqual(header["retired"][0]["fallback"], {"court": fallback[0], "text_source": fallback[1], "decade": fallback[2]})
            self.assertEqual(rows[0]["opinion_id"], fixture.candidates(fallback)[0])
            self.assertEqual((rows[0]["court"], rows[0]["text_source"], rows[0]["decade"]), fallback)
            self.assertIn("fallback: ca6 html 2010", output)
            self.assertIn("rank: 1; checked and failed: 0", output)
            self.assertEqual(header["checked"]["ca6"], min(draw.CHECK_WINDOW, len(fixture.candidates(fallback))))

    def test_successor_exhausted_cell_falls_back_to_earlier_decade_on_tie(self) -> None:
        """Exhausted checks use distance, then the earlier decade."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _successor_fixture(Path(directory))
            original, fallback = ("ca6", "html", 2000), ("ca6", "html", 1990)
            candidates = fixture.candidates(original)
            _opinion_texts(fixture.stage, dict.fromkeys(candidates, EMPTY_HTML))
            _, header, rows, output = self._successor_sample(fixture, fixture.case_ids(original)[:1])
            self.assertEqual(header["retired"][0]["fallback"], {"court": fallback[0], "text_source": fallback[1], "decade": fallback[2]})
            self.assertEqual(rows[0]["opinion_id"], fixture.candidates(fallback)[0])
            self.assertEqual(rows[0]["decade"], fallback[2])
            self.assertIn("rank: 1; checked and failed: 0", output)
            self.assertEqual(header["checked"]["ca6"], len(candidates) + min(draw.CHECK_WINDOW, len(fixture.candidates(fallback))))

    def test_successor_refuses_when_all_fallback_candidates_fail(self) -> None:
        """Exhausted ingest checks refuse instead of admitting unreadable text."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _successor_fixture(Path(directory))
            candidates = [opinion_id for cell in fixture.orders if cell[0] == "ca6" for opinion_id in fixture.candidates(cell)]
            _opinion_texts(fixture.stage, dict.fromkeys(candidates, EMPTY_HTML))
            out = fixture.root / "refused.jsonl"
            with patch.object(opiniontext, "canonical_text", wraps=opiniontext.canonical_text) as canonical:
                result = _run_draw(fixture.arguments(out, fixture.case_ids(("ca6", "html", 2020))), fixture.root / "checkout")
            self._assert_refused(result, "check")
            self.assertEqual(canonical.call_count, len(candidates))
            self.assertFalse(out.exists())

    def test_successor_argument_and_retired_id_refusals(self) -> None:
        """The new form accepts only distinct listed unsuperseded segmenter ids."""

        for form in ("retire-write", "set-alone", "unknown", "repeated", "unlisted", "non-segmenter", "superseded"):
            with self.subTest(form=form), tempfile.TemporaryDirectory() as directory:
                fixture = _successor_fixture(Path(directory))
                cell = ("ca6", "html", 2000)
                retired = fixture.case_ids(cell)[0]
                out = fixture.root / "refused.jsonl"
                expected_stage = "retire"
                if form == "retire-write":
                    arguments = fixture.write_arguments(fixture.plain_sample) + ["--retire", retired]
                    expected_stage = "write"
                elif form == "set-alone":
                    arguments = fixture.arguments(out, ()) + ["--set", str(fixture.set_root)]
                    expected_stage = "draw"
                else:
                    requested = [retired]
                    if form == "unknown":
                        requested = [f"segmenter-{len(fixture.cases()) + 100:03d}"]
                    elif form == "non-segmenter":
                        requested = ["fictitious-other-case"]
                    elif form == "repeated":
                        requested *= 2
                    elif form == "unlisted":
                        for relative in (manifest.IDS_PATH, manifest.MIRROR_IDS_PATH):
                            path = fixture.set_root / relative
                            path.write_text("".join(case_id + "\n" for case_id in path.read_text(encoding="utf-8").splitlines()
                                                    if case_id != retired), encoding="utf-8")
                    elif form == "superseded":
                        fixture.append(fixture.candidates(cell)[2], cell, supersedes=retired)
                    arguments = fixture.arguments(out, requested)
                previous = {relative: (fixture.set_root / relative).read_bytes()
                            for relative in (manifest.CASE_PATH, manifest.IDS_PATH, manifest.MIRROR_IDS_PATH)}
                self._assert_refused(_run_draw(arguments, fixture.root / "checkout"), expected_stage)
                self.assertFalse(out.exists())
                for relative, raw in previous.items():
                    self.assertEqual((fixture.set_root / relative).read_bytes(), raw)

    def test_successor_write_appends_three_files_and_refuses_a_second_write(self) -> None:
        """Successor writes preserve every old byte and continue the highest id."""

        with tempfile.TemporaryDirectory() as directory:
            fixture = _successor_fixture(Path(directory))
            other_cell = ("ca6", "html", 1990)
            highest = max(int(cast(str, case["id"]).split("-")[1]) for case in fixture.cases()) + 5
            fixture.append(fixture.candidates(other_cell)[0], other_cell, ordinal=highest)
            cell = ("ca6", "html", 2000)
            sample, _, rows, _ = self._successor_sample(fixture, fixture.case_ids(cell)[:2])
            paths = (manifest.CASE_PATH, manifest.IDS_PATH, manifest.MIRROR_IDS_PATH)
            previous = {relative: (fixture.set_root / relative).read_bytes() for relative in paths}
            code, output, errors = _run_draw(fixture.write_arguments(sample), fixture.root / "checkout")
            self.assertEqual((code, errors), (0, ""), output)
            self._assert_content_free(output, errors)
            new_ids = [f"segmenter-{highest + index:03d}" for index in range(1, len(rows) + 1)]
            expected_cases = []
            for case_id, row in zip(new_ids, rows, strict=True):
                case = _case(case_id, row["opinion_id"])
                case.update({key: row[key] for key in ("court", "decade", "text_source")})
                case["review"] = {"by": "CSA-1", "on": "2001-02-03"}
                case["supersedes"] = row["supersedes"]
                expected_cases.append(case)
            case_bytes = (fixture.set_root / manifest.CASE_PATH).read_bytes()
            appended = case_bytes[len(previous[manifest.CASE_PATH]):]
            self.assertEqual(appended, "".join(json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n"
                                              for case in expected_cases).encode("utf-8"))
            for raw, expected in zip(appended.splitlines(), expected_cases, strict=True):
                self.assertEqual(tuple(json.loads(raw)), tuple(expected))
                self.assertEqual(tuple(json.loads(raw))[-1], "supersedes")
            appended_ids = "".join(case_id + "\n" for case_id in new_ids).encode("utf-8")
            current = {relative: (fixture.set_root / relative).read_bytes() for relative in paths}
            self.assertTrue(case_bytes.startswith(previous[manifest.CASE_PATH]))
            for relative in paths[1:]:
                self.assertEqual(current[relative], previous[relative] + appended_ids)
            self.assertEqual(current[manifest.IDS_PATH], current[manifest.MIRROR_IDS_PATH])
            self.assertEqual(output.splitlines(), [
                f"{name}: {len(current[relative].splitlines())} lines sha256 {hashlib.sha256(current[relative]).hexdigest()}"
                for name, relative in zip(("cases", "ids", "mirror ids"), paths, strict=True)
            ])
            loaded = evalset.load_set(fixture.set_root)
            self.assertTrue(loaded.ok, loaded.findings)
            self._assert_refused(_run_draw(fixture.write_arguments(sample), fixture.root / "checkout"), "retire")
            for relative in paths:
                self.assertEqual((fixture.set_root / relative).read_bytes(), current[relative])

    def test_successor_write_refuses_missing_final_newlines(self) -> None:
        """A malformed prefix must be restored before any append."""

        paths = (manifest.CASE_PATH, manifest.IDS_PATH, manifest.MIRROR_IDS_PATH)
        for relative in paths:
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                fixture = _successor_fixture(Path(directory))
                sample, _, _, _ = self._successor_sample(fixture, fixture.case_ids(("ca6", "html", 2000))[:1])
                path = fixture.set_root / relative
                path.write_bytes(path.read_bytes()[:-1])
                previous = {item: (fixture.set_root / item).read_bytes() for item in paths}
                result = _run_draw(fixture.write_arguments(sample), fixture.root / "checkout")
                self._assert_refused(result, "write")
                self.assertIn("newline", result[2])
                for item in paths:
                    self.assertEqual((fixture.set_root / item).read_bytes(), previous[item])

    def test_successor_failed_replacement_restores_every_file(self) -> None:
        """A second or third replacement failure restores all previous bytes."""

        atomic = manifest.write_atomic
        for failed_call in (2, 3):
            for after_replace in (False, True):
                with self.subTest(call=failed_call, after=after_replace), tempfile.TemporaryDirectory() as directory:
                    fixture = _successor_fixture(Path(directory))
                    sample, _, _, _ = self._successor_sample(fixture, fixture.case_ids(("ca6", "html", 2000))[:2])
                    previous = {relative: (fixture.set_root / relative).read_bytes()
                                for relative in (manifest.CASE_PATH, manifest.IDS_PATH, manifest.MIRROR_IDS_PATH)}
                    calls = 0

                    def fail(path: Path, payload: bytes, *, failed_call: int = failed_call,
                             after_replace: bool = after_replace) -> None:
                        nonlocal calls
                        calls += 1
                        if calls == failed_call and not after_replace:
                            raise OSError("fictitious replacement failure")
                        atomic(path, payload)
                        if calls == failed_call and after_replace:
                            raise OSError("fictitious cleanup failure")

                    with patch.object(manifest, "write_atomic", side_effect=fail):
                        result = _run_draw(fixture.write_arguments(sample), fixture.root / "checkout")
                    self._assert_refused(result, "write")
                    for relative, raw in previous.items():
                        self.assertEqual((fixture.set_root / relative).read_bytes(), raw)


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
        manifest.pin_slice(set_root),
        "segmenter-label@1", segmenter_draft.PROMPT_DIGESTS["segmenter-label@1"], cases,
    ), strict=True))
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return set_root, sheets, manifest_path, text


def _run_intake(manifest_path: Path, sheets: Path, *, set_root: Path | None = None,
                dry_run: bool = False, cases: tuple[str, ...] = ()) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    args = [str(manifest_path), str(sheets), "--signer", "CSA-1", "--on", "2026-10-08"]
    if set_root is not None:
        args.extend(("--set", str(set_root)))
    for case_id in cases:
        args.extend(("--case", case_id))
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
            self.assertEqual(output.splitlines()[-3:-1], [
                "set digest: unchanged since the draft", "not taken: none",
            ])
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
                code, output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
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
        scenarios = ("starts", "format-1", "format-1-keys", "changed-case", "reordered-cases", "reordered-ids")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                set_root, sheets, manifest_path, _text = _intake_fixture(Path(directory))
                if scenario in {"starts", "format-1", "format-1-keys"}:
                    value = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if scenario == "starts":
                        value["cases"][0]["starts"] = [0, 0]
                    else:
                        value["format"] = 1
                        if scenario == "format-1-keys":
                            del value["slice_pins"]
                    manifest_path.write_text(json.dumps(value), encoding="utf-8")
                    stage = "manifest"
                else:
                    path = set_root / (manifest.IDS_PATH if scenario == "reordered-ids" else manifest.CASE_PATH)
                    lines = path.read_bytes().splitlines(keepends=True)
                    if scenario == "changed-case":
                        case = json.loads(lines[0])
                        case["notes"] = "Fictitious change after the draft"
                        lines[0] = (json.dumps(case) + "\n").encode("utf-8")
                    else:
                        lines.reverse()
                    path.write_bytes(b"".join(lines))
                    stage = "set"
                code, output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
                self.assertEqual(code, 1, output)
                self.assertEqual(output, "")
                self.assertTrue(errors.startswith(f"{stage}: refused"), errors)
                self.assertIn("Fix:", errors)
                if stage == "set":
                    self.assertIn("the segmenter slice's files changed since the draft", errors)
                    self.assertIn("Draft the slice's unsigned cases again, then retry.", errors)
                else:
                    self.assertIn("manifest shape is invalid", errors)
                self.assertFalse(segmenter_labels.path_for(set_root).exists())

    def test_slice_pin_shape_refusals(self) -> None:
        case_key, ids_key = manifest.CASE_PATH.as_posix(), manifest.IDS_PATH.as_posix()
        valid_pin = [2, "a" * 64]
        invalid_pins: list[object] = [
            None, [], {case_key: valid_pin},
            {case_key: valid_pin, ids_key: valid_pin, "extra": valid_pin},
        ]
        invalid_values: tuple[object, ...] = (
            None, {}, [], [2], [2, "a" * 64, "extra"],
            [0, "a" * 64], [-1, "a" * 64], [True, "a" * 64],
            ["2", "a" * 64], [2.5, "a" * 64],
            [2, None], [2, "a" * 63], [2, "g" * 64],
        )
        invalid_pins.extend({case_key: pin, ids_key: valid_pin} for pin in invalid_values)
        for index, pins in enumerate(invalid_pins):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                set_root, sheets, manifest_path, _text = _intake_fixture(Path(directory))
                value = json.loads(manifest_path.read_text(encoding="utf-8"))
                value["slice_pins"] = pins
                manifest_path.write_text(json.dumps(value), encoding="utf-8")
                code, output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
                self.assertEqual((code, output), (1, ""))
                self.assertIn("manifest: refused — manifest shape is invalid", errors)
                self.assertIn("Fix:", errors)
                self.assertFalse(segmenter_labels.path_for(set_root).exists())

    def test_appends_pass_the_hold_and_report_the_digest(self) -> None:
        for location in ("non-slice", "slice"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory:
                set_root, sheets, manifest_path, _text = _intake_fixture(Path(directory))
                if location == "non-slice":
                    path = set_root / "build-gates/fictitious-other.jsonl"
                    path.write_text(json.dumps(_case("segmenter-003", 103)) + "\n", encoding="utf-8")
                    result = evalset.load_set(set_root)
                    self.assertTrue(result.ok, result.findings)
                    assert result.loaded is not None
                    value = json.loads(manifest_path.read_text(encoding="utf-8"))
                    value["set_digest"] = result.loaded.digest
                    manifest_path.write_text(json.dumps(value), encoding="utf-8")
                    added = _case("segmenter-004", 104)
                else:
                    path = set_root / manifest.CASE_PATH
                    added = _case("segmenter-003", 103)
                    ids_path = set_root / manifest.IDS_PATH
                    ids_path.write_bytes(ids_path.read_bytes() + f"{added['id']}\n".encode())
                path.write_bytes(path.read_bytes() + (json.dumps(added) + "\n").encode("utf-8"))
                code, output, errors = _run_intake(manifest_path, sheets, set_root=set_root)
                self.assertEqual((code, errors), (0, ""), output)
                self.assertEqual(output.splitlines()[-3:-1], [
                    "set digest: moved since the draft", "not taken: none",
                ])
                value = json.loads(manifest_path.read_text(encoding="utf-8"))
                parsed = segmenter_labels.parse_bytes(segmenter_labels.path_for(set_root).read_bytes())
                self.assertFalse(parsed.findings)
                self.assertEqual(
                    [item.record.case_id for item in parsed.records],
                    [case["case_id"] for case in value["cases"]],
                )

    def test_case_selection_batches_and_already_labelled_refusal(self) -> None:
        """Unsigned cases are checked and appended in separate batches."""

        with tempfile.TemporaryDirectory() as directory:
            set_root, sheets, manifest_path, text = _intake_fixture(Path(directory))
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            first, second = (case["case_id"] for case in value["cases"])
            omitted_sheet = sheets / f"{second}.txt"
            saved_sheet = omitted_sheet.read_bytes()
            omitted_sheet.unlink()
            label_path = segmenter_labels.path_for(set_root)
            code, output, errors = _run_intake(manifest_path, sheets, cases=(first, first), dry_run=True)
            self.assertEqual((code, errors), (0, ""), output)
            self.assertIn(f"not taken: 1 ids {second}\n", output)
            self.assertNotIn(f"{second}:", output)
            self.assertFalse(label_path.exists())
            code, output, errors = _run_intake(manifest_path, sheets, cases=(first, first))
            self.assertEqual((code, errors), (0, ""), output)
            self.assertIn(f"not taken: 1 ids {second}\n", output)
            before = label_path.read_bytes()
            parsed = segmenter_labels.parse_bytes(before)
            self.assertFalse(parsed.findings)
            self.assertEqual([item.record.case_id for item in parsed.records], [first])
            omitted_sheet.write_bytes(saved_sheet)
            first_sheet = sheets / f"{first}.txt"
            saved_first_sheet = first_sheet.read_bytes()
            first_sheet.unlink()
            code, output, errors = _run_intake(manifest_path, sheets, cases=(second,))
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(output.splitlines()[-3:-1], [
                "set digest: moved since the draft", f"not taken: 1 ids {first}",
            ])
            payload = label_path.read_bytes()
            self.assertTrue(payload.startswith(before))
            parsed = segmenter_labels.parse_bytes(payload)
            self.assertFalse(parsed.findings)
            self.assertEqual([item.record.case_id for item in parsed.records], [first, second])
            first_sheet.write_bytes(saved_first_sheet)
            code, output, errors = _run_intake(manifest_path, sheets, cases=(first, second))
            self.assertEqual((code, output), (1, ""))
            self.assertTrue(errors.startswith(f"set: refused — case {first} is no longer active and unsigned"), errors)
            self.assertIn("Fix:", errors)
            self.assertEqual(label_path.read_bytes(), payload)
            self.assertNotIn(text, output + errors)

    def test_later_batch_relocates_by_set_version(self) -> None:
        """A checked batch lands from another checkout of the same set version."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            set_root, sheets, manifest_path, _text = _intake_fixture(root)
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            first, second = (case["case_id"] for case in value["cases"])
            code, output, errors = _run_intake(manifest_path, sheets, cases=(first,))
            self.assertEqual((code, errors), (0, ""), output)
            before = segmenter_labels.path_for(set_root).read_bytes()
            relocated = root / "later-checkout" / set_root.name
            shutil.copytree(set_root, relocated)
            wrong_version = relocated.with_name("eval-v1000")
            shutil.copytree(relocated, wrong_version)
            shutil.rmtree(set_root)
            code, output, errors = _run_intake(manifest_path, sheets, set_root=wrong_version, cases=(second,))
            self.assertEqual((code, output), (1, ""))
            self.assertTrue(errors.startswith("set: refused"), errors)
            self.assertIn(f"Use a set named {set_root.name}, then retry.", errors)
            self.assertEqual(segmenter_labels.path_for(wrong_version).read_bytes(), before)
            code, output, errors = _run_intake(manifest_path, sheets, set_root=relocated, cases=(second,))
            self.assertEqual((code, errors), (0, ""), output)
            self.assertEqual(output.splitlines()[-3:-1], [
                "set digest: moved since the draft", f"not taken: 1 ids {first}",
            ])
            payload = segmenter_labels.path_for(relocated).read_bytes()
            self.assertTrue(payload.startswith(before))
            parsed = segmenter_labels.parse_bytes(payload)
            self.assertFalse(parsed.findings)
            self.assertEqual([item.record.case_id for item in parsed.records], [first, second])

    def test_unknown_or_invalid_requested_case_refuses_with_fix(self) -> None:
        for case_id in ("segmenter-999", "fictitious-invalid-id"):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as directory:
                set_root, sheets, manifest_path, _text = _intake_fixture(Path(directory))
                code, output, errors = _run_intake(manifest_path, sheets, cases=(case_id,))
                self.assertEqual((code, output), (1, ""))
                self.assertIn(case_id, errors)
                self.assertIn("Name a case the manifest holds, then retry.", errors)
                self.assertFalse(segmenter_labels.path_for(set_root).exists())


class SlicePins(unittest.TestCase):
    """The holds use the slice files' byte prefixes and allow appends."""

    def test_pins_and_holds_match_committed_file_prefix_arithmetic(self) -> None:
        set_root = Path(__file__).resolve().parents[1] / evalset.SET_ROOT
        expected: dict[str, tuple[int, str]] = {}
        prefixes: dict[str, tuple[int, str]] = {}
        for path in (manifest.CASE_PATH, manifest.IDS_PATH):
            lines = (set_root / path).read_bytes().splitlines(keepends=True)
            count = len(lines)
            expected[path.as_posix()] = (count, hashlib.sha256(b"".join(lines[:count])).hexdigest())
            prefix_count = count // 2
            self.assertGreater(prefix_count, 0)
            prefixes[path.as_posix()] = (prefix_count, hashlib.sha256(b"".join(lines[:prefix_count])).hexdigest())
        self.assertEqual(manifest.pin_slice(set_root), expected)
        self.assertTrue(manifest.hold_slice(set_root, expected))
        self.assertTrue(manifest.hold_slice(set_root, prefixes))

        with tempfile.TemporaryDirectory() as directory:
            copy_root = Path(directory)
            for path in (manifest.CASE_PATH, manifest.IDS_PATH):
                target = copy_root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((set_root / path).read_bytes() + b"Fictitious appended line\n")
            self.assertTrue(manifest.hold_slice(copy_root, expected))
            self.assertTrue(manifest.hold_slice(copy_root, prefixes))

    def test_hold_refuses_truncation_and_missing_final_newline(self) -> None:
        set_root = Path(__file__).resolve().parents[1] / evalset.SET_ROOT
        pins = manifest.pin_slice(set_root)
        for path in (manifest.CASE_PATH, manifest.IDS_PATH):
            for scenario in ("truncated", "no-newline", "appended-no-newline"):
                with self.subTest(path=path, scenario=scenario), tempfile.TemporaryDirectory() as directory:
                    copy_root = Path(directory)
                    for relative_path in (manifest.CASE_PATH, manifest.IDS_PATH):
                        target = copy_root / relative_path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes((set_root / relative_path).read_bytes())
                    raw = (copy_root / path).read_bytes()
                    if scenario == "truncated":
                        changed = b"".join(raw.splitlines(keepends=True)[:-1])
                        self.assertTrue(changed.endswith(b"\n"))
                    elif scenario == "no-newline":
                        self.assertTrue(raw.endswith(b"\n"))
                        changed = raw[:-1]
                    else:
                        changed = raw + b"Fictitious append without a newline"
                    (copy_root / path).write_bytes(changed)
                    self.assertFalse(manifest.hold_slice(copy_root, pins))


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
                            or node.module == "tools.synthetic.manifest"
                        )
                        self.assertFalse((node.module or "").startswith("gideon.worker"))

    def test_opiniontext_import_is_only_inside_a_draw_function(self) -> None:
        """Plain draws and writes do not load the worker's parser dependencies."""

        assert draw.__file__ is not None
        tree = ast.parse(Path(draw.__file__).read_text(encoding="utf-8"))
        imports: list[tuple[int, set[str]]] = []

        def visit(node: ast.AST, function_depth: int = 0) -> None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                function_depth += 1
            if isinstance(node, ast.ImportFrom) and node.module == "gideon.worker.opiniontext":
                imports.append((function_depth, {name.name for name in node.names}))
            elif isinstance(node, ast.Import) and any(name.name == "gideon.worker.opiniontext" for name in node.names):
                imports.append((function_depth, set()))
            for child in ast.iter_child_nodes(node):
                visit(child, function_depth)

        visit(tree)
        self.assertTrue(imports, "draw must use the worker's own opinion-text check")
        self.assertTrue(all(depth > 0 for depth, _ in imports), "worker imports must be inside a function")
        self.assertTrue({"OpinionTextFailure", "canonical_text", "choose"} <= set().union(*(names for _, names in imports)))


if __name__ == "__main__":
    unittest.main()
