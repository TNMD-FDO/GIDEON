"""The turn's exact-object extraction and lexical leg table."""

import asyncio
import io
import logging
import sys
import unittest
import warnings
from threading import Event, Lock
from types import ModuleType
from typing import cast
from unittest.mock import Mock, patch

from test_extraction_set import _available_case_files

from gideon.extraction import OBJECT_TYPES, ExactObject, contract, extract
from gideon.extraction.grammar import registry_types
from gideon.extraction.scoring import active_cases
from gideon.turn import objects
from gideon.turn.states import State, TurnFailure
from gideon.worker.logs import _DROPPED_LOGGERS


class LegTable(unittest.TestCase):
    def test_case_cite_sends_typed_then_indexed_spelling_to_both_searches(self) -> None:
        typed = "444 U. S. 987"
        message = f"[FICTIONAL TEST ONLY] {typed}"
        obj = ExactObject("case_cite", message.index(typed), len(message), typed)
        indexed = "444 U.S. 987"
        for spelling, values in (
            (indexed, (typed, indexed)), (typed, (typed,)), (None, (typed,)),
        ):
            with self.subTest(spelling=spelling):
                self.assertEqual(objects.object_leg(obj, spelling, message), (
                    objects.ObjectSearch("held", "caption", "phrase", values),
                    objects.ObjectSearch("citing", "cites", "term", values),
                ))

    def test_every_declared_extractor_type_has_a_leg(self) -> None:
        message = "[FICTIONAL TEST ONLY]"
        leg_types = set()
        for object_type in OBJECT_TYPES:
            key = "fixture/fictional-key" if object_type in contract.KEYED_TYPES else None
            obj = ExactObject(object_type, 0, len(message), message, key)
            if objects.object_leg(obj, None, message):
                leg_types.add(object_type)
        self.assertEqual(leg_types, set(registry_types()) | set(contract.EYECITE_TYPES))

    def test_leg_table_over_the_active_extraction_set(self) -> None:
        files = tuple(
            tuple(line.record for line in lines) for _, _, lines in _available_case_files(self)
        )
        for case in active_cases(files):
            message = cast(str, case["question"])
            expected = cast(dict[str, list[dict[str, object]]], case["expected"])
            for label in expected["objects"]:
                obj = contract.object_from_wire({
                    "key": None, "pattern_id": None, "subsections": [], **label,
                })
                with self.subTest(case=case["id"], type=obj.type, start=obj.start):
                    typed = (message[obj.start:obj.end],)
                    leg: tuple[objects.ObjectSearch, ...]
                    match obj.type:
                        case "case_cite":
                            leg = (
                                objects.ObjectSearch("held", "caption", "phrase", typed),
                                objects.ObjectSearch("citing", "cites", "term", typed),
                            )
                        case "statute" | "guideline" | "court_rule" | "regulation" | "appendix_statute" | "habeas_rule" | "scotus_rule":
                            assert obj.key is not None
                            leg = (objects.ObjectSearch("matched", "cites", "term", (obj.key,)),)
                        case "bare_section" | "bare_rule":
                            leg = (objects.ObjectSearch("matched", "text", "phrase", typed),)
                        case "docket":
                            leg = (objects.ObjectSearch("matched", "caption", "phrase", typed),)
                        case "caption" | "state_code":
                            leg = ()
                        case _:
                            self.fail("Missing expected leg for labelled object type.")
                    self.assertEqual(objects.object_leg(obj, None, message), leg)


class Loader(unittest.TestCase):
    def setUp(self) -> None:
        self.caught_warnings = self.enterContext(warnings.catch_warnings(record=True))
        warnings.simplefilter("always")
        for name in objects.CITATION_LOGGERS:
            logger = logging.getLogger(name)
            self.enterContext(patch.object(logger, "handlers", logger.handlers.copy()))
            self.enterContext(patch.object(logger, "propagate", logger.propagate))

    def test_import_error_returns_no_extractor(self) -> None:
        with patch.dict(sys.modules, {"gideon.casecite.adapter": None}):
            self.assertIsNone(objects.load_case_extractor())

    def test_diagnostics_are_dropped_before_import(self) -> None:
        self.assertEqual(objects.CITATION_LOGGERS, _DROPPED_LOGGERS - {"httpx", "httpcore"})
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        adapter = Mock(return_value=())
        fake_module = ModuleType("gideon.casecite.adapter")

        def emit_diagnostics() -> None:
            for name in objects.CITATION_LOGGERS:
                for module in (name, f"{name}.fictional.child"):
                    logging.getLogger(module).error("[FICTIONAL TEST ONLY] citation log")
                    warnings.warn_explicit(
                        "[FICTIONAL TEST ONLY] citation warning", UserWarning,
                        "fictional.py", 1, module=module,
                    )

        def import_entry(name: str) -> objects.CaseExtractor:
            if name != "extract_indexed_case_cites":
                raise AttributeError(name)
            emit_diagnostics()
            return adapter

        fake_module.__dict__["__getattr__"] = import_entry
        with (
            patch.dict(sys.modules, {"gideon.casecite.adapter": fake_module}),
            patch.object(logging.getLogger(), "handlers", [handler]),
            patch.object(logging, "lastResort", handler),
        ):
            self.assertIs(objects.load_case_extractor(), adapter)
            with patch.object(logging.getLogger(), "handlers", []):
                emit_diagnostics()
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(self.caught_warnings, [])


class Extraction(unittest.TestCase):
    def test_grammar_wins_the_overlap_and_objects_keep_message_order(self) -> None:
        typed = "444 U. S. 987"
        message = f"[FICTIONAL TEST ONLY] {typed}; 18 U.S.C. § 9999"
        (statute,) = extract(message)
        overlap = ExactObject("case_cite", statute.start, statute.end, statute.text)
        start = message.index(typed)
        cite = ExactObject("case_cite", start, start + len(typed), typed)
        indexed = "444 U.S. 987"

        extracted = asyncio.run(objects.extract_objects(
            message, lambda _: ((overlap, None), (cite, indexed)),
        ))

        self.assertEqual(extracted, (
            objects.TurnObject(0, cite, indexed),
            objects.TurnObject(1, statute, None),
        ))

    def test_contract_violation_is_refused(self) -> None:
        message = "[FICTIONAL TEST ONLY]"
        invalid = ExactObject("case_cite", 0, len(message), "fictional wrong span")
        with self.assertRaises(TurnFailure) as raised:
            asyncio.run(objects.extract_objects(message, lambda _: ((invalid, None),)))
        self.assertEqual(raised.exception.state, State.EXTRACTION_FAILED)
        self.assertEqual(raised.exception.word, "extraction")
        self.assertEqual(raised.exception.status, 500)

    def test_timeout_during_adapter_call(self) -> None:
        started = Event()
        finish = Event()

        def adapter(_: str) -> tuple[tuple[ExactObject, str | None], ...]:
            started.set()
            finish.wait(3)
            return ()

        async def run() -> None:
            try:
                with self.assertRaises(TurnFailure) as raised:
                    await objects.extract_objects("[FICTIONAL TEST ONLY]", adapter)
                self.assertEqual(raised.exception.state, State.RETRIEVAL_TIMEOUT)
                self.assertEqual(raised.exception.word, "extraction")
            finally:
                finish.set()

        with patch.object(objects, "EXTRACTION_TIMEOUT_SECONDS", 0.25):
            asyncio.run(run())
        self.assertTrue(started.is_set())

    def test_expired_lock_waiter_never_invokes_adapter(self) -> None:
        lock = Lock()
        waiting = Event()
        adapter = Mock(return_value=())
        observed_lock = Mock(wraps=lock)

        def acquire(*, timeout: float) -> bool:
            waiting.set()
            return lock.acquire(timeout=timeout)

        observed_lock.acquire.side_effect = acquire

        async def run() -> None:
            with lock, self.assertRaises(TurnFailure) as raised:
                await objects.extract_objects("[FICTIONAL TEST ONLY]", adapter)
            self.assertEqual(raised.exception.state, State.RETRIEVAL_TIMEOUT)
            self.assertEqual(raised.exception.word, "extraction")

        with (
            patch.object(objects, "_EXTRACTION_LOCK", observed_lock),
            patch.object(objects, "EXTRACTION_TIMEOUT_SECONDS", 0.25),
        ):
            asyncio.run(run())
        self.assertTrue(waiting.is_set())
        adapter.assert_not_called()
        self.assertFalse(lock.locked())
