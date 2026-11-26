"""Pool coordinates resolve through separate metrics-role record reads."""

import json
import subprocess
import unittest
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_evaluation_baselines import assert_import_boundary
from test_evaluation_record import read_psql_set
from test_research_door import turn_pool_fixture
from test_turns_door import FakeHost

from gideon.evaluation import pool_coordinates, record
from gideon.evaluation.results import RunContext
from gideon.host import stack
from gideon.host.report import Problem

PRODUCTION = Path("/fictitious/production-rendered")
SIBLING = Path("/fictitious/sibling-rendered")
SENTINEL = "Fictitious record diagnostic text"


def completed(*rows: object, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(("fixture",), returncode, "\n".join(map(json.dumps, rows)), SENTINEL)


class CoordinateReads(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeHost()
        self.context = RunContext(
            self.host, SIBLING, PRODUCTION, served_model_name=None,
            judge_prompt_id=None, repeats=1, progress=lambda _: None,
        )
        data = turn_pool_fixture("coordinates.json")
        self.chunks: dict[str, dict[str, Any]] = {row["chunk_id"]: row for row in data["chunks"]}
        self.manifest: dict[str, dict[str, Any]] = {row["doc_id"]: row for row in data["manifest"]}
        reply = turn_pool_fixture("reply.json")
        self.name = reply["generation"]["gen_id"]
        self.ordered_ids = tuple(
            entry["chunk_id"] for entry in sorted(reply["pool"], key=lambda entry: entry["pool_rank"])
        )

    def respond(self, argv: list[str], *, input: str) -> subprocess.CompletedProcess[str]:
        if argv == record.reader_argv(PRODUCTION):
            self.assertIn("FROM public.chunks", input)
            self.assertIn("SELECT chunk_id, doc_id, char_start, char_end", input)
            self.assertIn("WHERE chunk_id IN", input)
            self.assertNotIn("public.generation_documents", input)
            rows = self.chunks
        else:
            self.assertEqual(argv, record.reader_argv(SIBLING))
            self.assertIn("FROM public.generation_documents", input)
            self.assertIn("SELECT doc_id, source_id, canonical_text_sha256", input)
            self.assertIn("WHERE gen_id = :'v_name' AND doc_id IN", input)
            line = next(line for line in input.splitlines() if line.startswith("\\set v_name "))
            self.assertEqual(read_psql_set(line, "v_name"), self.name)
            self.assertNotIn(self.name, argv)
            self.assertNotIn("public.chunks", input)
            rows = self.manifest
        batches = [
            json.loads(read_psql_set(line, "v_ids"))
            for line in input.splitlines() if line.startswith("\\set v_ids ")
        ]
        self.assertEqual(input.count("jsonb_array_elements_text(:'v_ids'::jsonb)"), len(batches))
        self.assertEqual(input.count("SELECT row_to_json("), len(batches))
        self.assertNotIn("JOIN", input.upper())
        for batch in batches:
            self.assertTrue(0 < len(batch) <= pool_coordinates.BATCH_SIZE)
            self.assertFalse(set(batch).intersection(argv))
        return completed(*(rows[key] for batch in batches for key in batch if key in rows))

    def test_two_stacks_read_each_distinct_id_in_bounded_batches_and_keep_rank_order(self) -> None:
        pools = {"fictitious-a": self.ordered_ids, "fictitious-b": self.ordered_ids[2::-1]}
        batch_size = min(len(self.chunks), len(self.manifest)) // 2
        with (
            patch.object(pool_coordinates, "BATCH_SIZE", batch_size),
            patch.object(self.host, "run", side_effect=self.respond) as run,
        ):
            result = pool_coordinates.read_coordinates(self.name, pools, self.context)

        self.assertEqual(run.call_count, 2)
        expected_ids = (
            (PRODUCTION, set(self.ordered_ids)),
            (SIBLING, {self.chunks[key]["doc_id"] for key in self.ordered_ids}),
        )
        for call, (directory, ids) in zip(run.call_args_list, expected_ids, strict=True):
            self.assertEqual(call.args[0], record.reader_argv(directory))
            bindings = [
                json.loads(read_psql_set(line, "v_ids"))
                for line in call.kwargs["input"].splitlines() if line.startswith("\\set v_ids ")
            ]
            self.assertGreater(len(bindings), 1)
            self.assertEqual(Counter(key for batch in bindings for key in batch), Counter(ids))

        coordinates = {
            key: (
                self.manifest[row["doc_id"]]["source_id"],
                self.manifest[row["doc_id"]]["canonical_text_sha256"], row["char_start"], row["char_end"],
            )
            for key, row in self.chunks.items()
        }
        self.assertEqual(result, {case: tuple(coordinates[key] for key in pool) for case, pool in pools.items()})

    def test_absent_chunk_and_absent_manifest_row_leave_unresolved_ranks(self) -> None:
        missing, unheld = self.ordered_ids[:2]
        unheld_doc = self.chunks[unheld]["doc_id"]
        held = next(
            key for key in self.ordered_ids[2:] if self.chunks[key]["doc_id"] != unheld_doc
        )
        held_chunk = self.chunks[held]
        held_doc = self.manifest[held_chunk["doc_id"]]
        del self.chunks[missing]
        del self.manifest[unheld_doc]
        pools = {"fictitious": (missing, held, unheld), "fictitious-empty": ()}
        with patch.object(self.host, "run", side_effect=self.respond):
            result = pool_coordinates.read_coordinates(self.name, pools, self.context)

        coordinate = (
            held_doc["source_id"], held_doc["canonical_text_sha256"], held_chunk["char_start"], held_chunk["char_end"],
        )
        self.assertEqual(result, {"fictitious": (None, coordinate, None), "fictitious-empty": ()})

    def test_empty_pools_skip_the_record(self) -> None:
        with patch.object(self.host, "run") as run:
            result = pool_coordinates.read_coordinates(self.name, {"fictitious-empty": ()}, self.context)
        run.assert_not_called()
        self.assertEqual(result, {"fictitious-empty": ()})

    def assert_problem(self, result: object, directory: Path, detail: str) -> None:
        assert isinstance(result, Problem)
        self.assertEqual(result.problem, detail)
        self.assertIn(stack.logs_fix(directory, record.POSTGRES_SERVICE), result.fix)
        self.assertNotIn(SENTINEL, repr(result))

    def test_chunk_read_refuses_invalid_foreign_and_duplicate_rows(self) -> None:
        chunk_id, other = self.ordered_ids[:2]
        row = self.chunks[chunk_id]
        cases = (
            subprocess.CompletedProcess(("fixture",), 0, SENTINEL, ""),
            completed([row]),
            completed({**row, "extra": SENTINEL}),
            completed({**row, "chunk_id": SENTINEL}),
            completed({**row, "doc_id": SENTINEL}),
            completed({**row, "char_start": True}),
            completed({**row, "char_start": -1}),
            completed({**row, "char_end": row["char_start"]}),
            completed(self.chunks[other]),
            completed(row, row),
        )
        for index, output in enumerate(cases):
            with self.subTest(index=index), patch.object(self.host, "run", return_value=output) as run:
                result = pool_coordinates.read_coordinates(self.name, {"fictitious": (chunk_id,)}, self.context)
                run.assert_called_once()
                self.assert_problem(result, PRODUCTION, "pool chunk row is invalid")

    def test_manifest_read_refuses_invalid_foreign_and_ambiguous_rows(self) -> None:
        chunk_id = self.ordered_ids[0]
        chunk = self.chunks[chunk_id]
        row = self.manifest[chunk["doc_id"]]
        other = next(value for key, value in self.manifest.items() if key != chunk["doc_id"])
        cases = (
            subprocess.CompletedProcess(("fixture",), 0, SENTINEL, ""),
            completed([row]),
            completed({**row, "extra": SENTINEL}),
            completed({**row, "doc_id": SENTINEL}),
            completed({**row, "source_id": SENTINEL}),
            completed({**row, "canonical_text_sha256": SENTINEL}),
            completed(other),
            completed(row, row),
            completed(row, {**row, "source_id": "fictitious:second-source"}),
        )
        for index, output in enumerate(cases):
            with self.subTest(index=index), patch.object(self.host, "run", side_effect=(completed(chunk), output)):
                result = pool_coordinates.read_coordinates(self.name, {"fictitious": (chunk_id,)}, self.context)
                self.assert_problem(result, SIBLING, "generation manifest row is invalid")

    def test_duplicates_across_batches_are_refused_on_both_records(self) -> None:
        first_id = self.ordered_ids[0]
        first = self.chunks[first_id]
        second_id = next(key for key in self.ordered_ids if self.chunks[key]["doc_id"] != first["doc_id"])
        second = self.chunks[second_id]
        document, other = (self.manifest[row["doc_id"]] for row in (first, second))
        cases = (
            ((completed(first, second, first),), PRODUCTION, "pool chunk row is invalid"),
            ((completed(first, second), completed(document, other, document)), SIBLING, "generation manifest row is invalid"),
        )
        for outputs, directory, detail in cases:
            with (
                self.subTest(directory=directory),
                patch.object(pool_coordinates, "BATCH_SIZE", 1),
                patch.object(self.host, "run", side_effect=outputs),
            ):
                result = pool_coordinates.read_coordinates(self.name, {"fictitious": (first_id, second_id)}, self.context)
                self.assert_problem(result, directory, detail)

    def test_unavailable_or_nonzero_reads_refuse_with_their_own_stack_fix(self) -> None:
        chunk_id = self.ordered_ids[0]
        chunk = self.chunks[chunk_id]
        document = self.manifest[chunk["doc_id"]]
        failed_exit = 23
        cases = (
            ((OSError(SENTINEL),), PRODUCTION, "pool chunk read could not run"),
            ((completed(chunk, returncode=failed_exit),), PRODUCTION, f"pool chunk read failed (exit {failed_exit})"),
            ((completed(chunk), OSError(SENTINEL)), SIBLING, "generation manifest read could not run"),
            ((completed(chunk), completed(document, returncode=failed_exit)), SIBLING, f"generation manifest read failed (exit {failed_exit})"),
        )
        for outputs, directory, detail in cases:
            with self.subTest(directory=directory, detail=detail), patch.object(self.host, "run", side_effect=outputs) as run:
                result = pool_coordinates.read_coordinates(self.name, {"fictitious": (chunk_id,)}, self.context)
                self.assert_problem(result, directory, detail)
                self.assertEqual(run.call_count, len(outputs))

    def test_imports_stay_within_record_coordinates_context_and_host_seams(self) -> None:
        assert_import_boundary(self, pool_coordinates, {
            "gideon.evaluation", "gideon.evaluation.record", "gideon.evaluation.rankmetrics", "gideon.evaluation.results",
            "gideon.host", "gideon.host.index.sample", "gideon.host.render.worker", "gideon.host.report", "gideon.host.stack",
            "gideon.host.staging", "gideon.host.sysio", "gideon.host.worker",
        })
