"""The turn row's projections hold ids, words, and numbers alone, and its writer stays silent."""

import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch

from gideon import __version__, guardrail
from gideon.evaluation import evalset
from gideon.turn import turnlog
from gideon.turn.finalset import FinalHit

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "migrations/0034_turn_log.sql"
SENTINEL = "Fictitious free text: the violet harbor gate"


class TurnLog(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatch = self.enterContext(patch.object(guardrail, "dispatch_event_row"))
        self.turn = turnlog.TurnLog(
            source=guardrail.EVAL_SOURCE, chat_id="fixture-chat_1", message_id="fixture-message_2",
            stop_after=None, retrieval_config_hash="fixture-config-hash", overrides=None, plan_mode="off",
        )

    def test_dispatched_columns_follow_the_migration_in_order(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        ddl = re.search(r"CREATE TABLE IF NOT EXISTS (\w+) \(\n(.*?)\n\) PARTITION", sql, re.DOTALL)
        partition = re.search(r"CREATE OR REPLACE FUNCTION (\w+)\(", sql)
        assert ddl is not None and partition is not None
        id_column, at_column, *columns = re.findall(r"^    ([a-z_]+) [a-z]+", ddl[2], re.MULTILINE)
        self.assertEqual((id_column, at_column), ("id", "at"))
        self.assertEqual(turnlog.TURN_LOG_COLUMNS, tuple(columns))

        turnlog.record_turn(self.turn, elapsed_ms=lambda: 17.0)
        self.dispatch.assert_called_once()
        table, function, names, values = self.dispatch.call_args.args
        self.assertEqual((table, function, names), (ddl[1], partition[1], tuple(columns)))
        self.assertEqual(len(values), len(columns))

    def test_projects_the_row_without_free_text(self) -> None:
        self.turn.chat_id = SENTINEL
        self.turn.message_id = "x" * 129
        provenance = {"plan_mode": self.turn.plan_mode, "legs": {"dense": {"returned": 2}}}
        objects = [
            {"index": 0, "type": "reporter", "resolved": "fixture-court", "hits": 2, "text": SENTINEL},
            {"index": 1, "type": SENTINEL, "resolved": {"text": SENTINEL}},
            {"index": 2, "type": "case_cite", "resolved": "unresolved_in_scope", "citing_hits": 2},
            {"type": "court", "resolved": "fixture-circuit"},
        ]
        self.turn.provenance = {**provenance, "out_of_corpus_objects": objects}
        entry = {
            "chunk_id": "fixture-chunk", "pool_rank": 1, "score": 0.25, "leg_ranks": {"dense": 1, "lexical": 2},
            "dupe_cluster": "fixture-cluster", "boilerplate": False, "pinned": True, "object": 0,
            "authority_tier": "circuit_published",
            "reserved": "controlling", "controlling_rank": 2,
            "rank": 2, "stage2_score": 0.0,
        }
        self.turn.pool = [
            {**entry, "text": SENTINEL},
            {
                **entry, "chunk_id": SENTINEL, "dupe_cluster": SENTINEL, "authority_tier": "fixture-unrecognized-tier",
                "reserved": SENTINEL, "controlling_rank": True, "rank": 1, "stage2_score": float("nan"),
            },
        ]
        hit = FinalHit(7, "fixture-chunk", 1, 2, 0.0, "circuit_published", "pin", 0, "fixture-opinion", 3)
        self.turn.evidence = [hit, SimpleNamespace(
            number=0, chunk_id=SENTINEL, pool_rank=True, rank=-1, stage2_score=float("nan"), authority_tier=SENTINEL,
            seat=SENTINEL, object=False, doc_id=SENTINEL, tokens=0, caption=SENTINEL, text=SENTINEL,
        )]
        self.turn.engine_counters = [
            turnlog.AttemptCounters(True, "length", 12, 7, 3),
            turnlog.AttemptCounters(False, SENTINEL, -1, 4),
        ]

        turnlog.record_turn(self.turn, elapsed_ms=lambda: 17.0)
        self.dispatch.assert_called_once()
        _, _, columns, values = self.dispatch.call_args.args
        row = dict(zip(columns, values, strict=True))
        self.assertNotIn(SENTINEL, repr(self.dispatch.call_args.args))
        self.assertIsNone(row["chat_id"])
        self.assertIsNone(row["message_id"])
        self.assertEqual(json.loads(row["provenance"]), provenance)
        self.assertEqual(self.turn.provenance["out_of_corpus_objects"], objects)
        self.assertEqual(json.loads(row["out_of_corpus_objects"]), [
            {"type": "reporter", "resolved": "fixture-court"},
            {"type": "unknown", "resolved": "unresolved"},
            {"type": "case_cite", "resolved": "unresolved_in_scope"},
            {"type": "court", "resolved": "fixture-circuit"},
        ])
        self.assertEqual(json.loads(row["pool"]), [
            entry, {**entry, "chunk_id": None, "dupe_cluster": None, "authority_tier": None,
                    "reserved": None, "controlling_rank": None, "rank": 1, "stage2_score": None},
        ])
        self.assertEqual(json.loads(row["evidence"]), [hit.to_wire(), dict.fromkeys(hit.to_wire())])
        self.assertEqual(json.loads(row["engine_counters"]), [
            {"thinking": True, "finish_reason": "length", "prompt_tokens": 12, "completion_tokens": 7, "reasoning_tokens": 3},
            {"thinking": False, "finish_reason": None, "prompt_tokens": None, "completion_tokens": 4, "reasoning_tokens": None},
        ])
        self.assertEqual((row["support_level"], row["gate_report"], row["misses"]), (None, None, None))

    def test_evidence_validates_seat_score_and_token_count(self) -> None:
        hit = FinalHit(1, "fixture-chunk", 2, 1, 1.0, "state_home", "controlling", None, "fixture-opinion", 3)
        cases = (
            ("controlling", 1, 3, "controlling", 1, 3),
            ("score", None, 1, "score", None, 1),
            ("fixture-seat", 1.1, -1, None, None, None),
            (True, True, True, None, None, None),
            ("pin", -0.1, 2.0, "pin", None, None),
            ("score", "0.5", "3", "score", None, None),
        )
        self.turn.evidence = [SimpleNamespace(**{
            **hit.to_wire(), "seat": seat, "stage2_score": score, "tokens": tokens,
        }) for seat, score, tokens, *_ in cases]
        turnlog.record_turn(self.turn, elapsed_ms=lambda: 17.0)
        self.dispatch.assert_called_once()
        _, _, columns, values = self.dispatch.call_args.args
        evidence = json.loads(dict(zip(columns, values, strict=True))["evidence"])
        self.assertEqual([(item["seat"], item["stage2_score"], item["tokens"]) for item in evidence], [
            (seat, score, tokens) for _, _, _, seat, score, tokens in cases
        ])

    def test_controlling_rank_requires_a_positive_integer(self) -> None:
        values = (None, 0, -1, True, 2.0, "2", 2)
        self.turn.pool = [{"controlling_rank": value} for value in values]
        turnlog.record_turn(self.turn, elapsed_ms=lambda: 17.0)
        self.dispatch.assert_called_once()
        _, _, columns, values_written = self.dispatch.call_args.args
        row = dict(zip(columns, values_written, strict=True))
        self.assertEqual([entry["controlling_rank"] for entry in json.loads(row["pool"])], [None] * (len(values) - 1) + [2])

    def test_outcome_vocabulary_and_unset_outcome(self) -> None:
        self.assertEqual(set(get_args(turnlog.Outcome)), {"reply", "complete", "truncated", "tripped", "failed", "disconnected"})
        turnlog.record_turn(self.turn, elapsed_ms=lambda: 17.0)
        self.dispatch.assert_called_once()
        _, _, columns, values = self.dispatch.call_args.args
        row = dict(zip(columns, values, strict=True))
        self.assertEqual(row["outcome"], "disconnected")
        self.assertEqual(json.loads(row["timings_ms"]), {"total": 17.0})

    def test_builder_failure_is_silent_and_dispatches_nothing(self) -> None:
        with patch.object(turnlog, "product_version", side_effect=RuntimeError(SENTINEL)):
            turnlog.record_turn(self.turn, elapsed_ms=lambda: 17.0)
        self.dispatch.assert_not_called()

    def test_dispatch_failure_is_silent(self) -> None:
        self.dispatch.side_effect = RuntimeError(SENTINEL)
        turnlog.record_turn(self.turn, elapsed_ms=lambda: 17.0)
        self.dispatch.assert_called_once()


class Versions(unittest.TestCase):
    def test_product_version_is_read_from_the_initializer_each_time(self) -> None:
        self.assertEqual(turnlog.product_version(), __version__)
        initializer = Path(self.enterContext(tempfile.TemporaryDirectory())) / "__init__.py"
        with patch.object(turnlog, "_PRODUCT_INITIALIZER", initializer):
            for version in ("9000.0.1", "9000.0.2"):
                with self.subTest(version=version):
                    initializer.write_text(f'"""Fictitious product initializer."""\n__version__ = {version!r}\n', encoding="utf-8")
                    self.assertEqual(turnlog.product_version(), version)

    def test_product_version_is_unknown_for_unreadable_or_misshapen_initializers(self) -> None:
        initializer = Path(self.enterContext(tempfile.TemporaryDirectory())) / "__init__.py"
        with patch.object(turnlog, "_PRODUCT_INITIALIZER", initializer):
            self.assertEqual(turnlog.product_version(), "unknown")
            for body in (b"\xff", b"__version__ =", b"__version__ = 42", b"# __version__ = '9000.0.1'"):
                with self.subTest(body=body):
                    initializer.write_bytes(body)
                    self.assertEqual(turnlog.product_version(), "unknown")

    def test_eval_set_version_matches_its_owner(self) -> None:
        self.assertEqual(turnlog.EVAL_SET_VERSION, evalset.EVAL_SET_VERSION)


class Imports(unittest.TestCase):
    def test_module_imports_only_standard_library_guardrail_and_tier_vocabulary(self) -> None:
        path = ROOT / "gideon/turn/turnlog.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                assert node.module is not None
                names = [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                with self.subTest(name=name):
                    self.assertTrue(
                        name.split(".")[0] in sys.stdlib_module_names
                        or name == "gideon.guardrail" or name.startswith("gideon.guardrail.")
                        or name == "gideon.turn.tier.AUTHORITY_TIERS",
                    )
