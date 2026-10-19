"""A turn's store addresses stay on the generation read from the record."""

import json
import unittest
from dataclasses import asdict

from test_index_record import NAME, RENDERED, SPACE, FakeHost

from gideon.host import report, worker
from gideon.host.corpus.record import database_fix
from gideon.host.index.generations import generation_name
from gideon.host.index.record import PromotedRow
from gideon.host.index.serving import (
    ServingGeneration,
    StoreNames,
    read_serving,
    select_serving,
    store_names,
)
from gideon.host.report import Problem


class Selection(unittest.TestCase):
    """Only the requested collection's sole live row can serve a turn."""

    def test_no_live_row_for_the_collection_is_unavailable(self) -> None:
        for rows in ((), (PromotedRow("fictitious-other-generation", "other", 4096),)):
            with self.subTest(rows=rows):
                self.assertIsNone(select_serving(rows, "caselaw"))

    def test_one_matching_row_is_selected_among_other_collections(self) -> None:
        rows = (
            PromotedRow("fictitious-other-generation", "other", 4096),
            PromotedRow(NAME, "caselaw", 8192),
        )
        self.assertEqual(select_serving(rows, "caselaw"), ServingGeneration(NAME, "caselaw"))

    def test_multiple_matching_rows_refuse_with_a_fix(self) -> None:
        first = PromotedRow(NAME, "caselaw", 8192)
        second = PromotedRow(generation_name("caselaw", "corpus-2099-02-03", SPACE), "caselaw", 16384)
        for rows in ((first, second), (first, first)):
            with self.subTest(rows=rows):
                result = select_serving(rows, "caselaw")
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("caselaw", result.problem)
                self.assertIn("multiple live generations", result.problem)
                self.assertIn("Postgres logs", result.fix)
                self.assertIn(report.command("index report"), result.fix)

    def test_both_store_addresses_are_the_generation_name(self) -> None:
        self.assertEqual(store_names(ServingGeneration(NAME, "caselaw")), StoreNames(NAME, NAME))


class Reader(unittest.TestCase):
    """One record read pins both stores across a promotion."""

    def test_turn_across_flip_keeps_old_addresses_and_next_turn_reads_new_generation(self) -> None:
        old = PromotedRow(NAME, "caselaw", 8192)
        new = PromotedRow(generation_name("caselaw", "corpus-2099-02-03", SPACE), "caselaw", 16384)
        host = FakeHost(stdout=json.dumps([asdict(old)]))
        turn = read_serving(host, RENDERED, "caselaw", command_path="index report")
        self.assertEqual(turn, ServingGeneration(old.gen_id, old.collection))
        assert isinstance(turn, ServingGeneration)
        self.assertEqual(len(host.calls), 1)

        # The record flips after the read, before either store query is addressed.
        host.stdout = json.dumps([asdict(new)])
        lexical = store_names(turn).lexical_index
        dense = store_names(turn).dense_collection
        self.assertEqual((lexical, dense), (old.gen_id, old.gen_id))
        self.assertEqual(len(host.calls), 1)

        next_turn = read_serving(host, RENDERED, "caselaw", command_path="index report")
        self.assertEqual(next_turn, ServingGeneration(new.gen_id, new.collection))
        assert isinstance(next_turn, ServingGeneration)
        next_names = store_names(next_turn)
        self.assertEqual(next_names, StoreNames(new.gen_id, new.gen_id))
        self.assertEqual(len(host.calls), 2)
        for name in (lexical, dense, next_names.lexical_index, next_names.dense_collection):
            self.assertNotEqual(name, "caselaw")
        for argv, _ in host.calls:
            self.assertEqual(argv, worker.psql_argv(RENDERED))

    def test_empty_read_and_duplicate_collection_rows(self) -> None:
        self.assertIsNone(read_serving(FakeHost(stdout="[]"), RENDERED, "caselaw", command_path="index report"))
        row = PromotedRow(NAME, "caselaw", 8192)
        host = FakeHost(stdout=json.dumps([asdict(row), asdict(row)]))
        self.assertEqual(
            read_serving(host, RENDERED, "caselaw", command_path="index report"),
            select_serving((row, row), "caselaw"),
        )
        self.assertEqual(len(host.calls), 1)

    def test_record_failure_is_returned_with_its_fix(self) -> None:
        host = FakeHost(code=3)
        self.assertEqual(
            read_serving(host, RENDERED, "caselaw", command_path="index report"),
            Problem("promoted generation read failed (exit 3)", database_fix(RENDERED, "index report")),
        )
        self.assertEqual(len(host.calls), 1)
