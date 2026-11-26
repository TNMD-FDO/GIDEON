"""Extract exact objects from the turn message and describe their lexical legs."""

import asyncio
import dataclasses
import logging
import re
import warnings
from collections.abc import Callable
from threading import Lock
from time import monotonic
from typing import Final, Literal

from gideon.extraction import contract, extract
from gideon.turn.states import State, TurnFailure

# exempt: a starting bound, corrected by the M1 reading.
EXTRACTION_TIMEOUT_SECONDS: Final = 5.0
CITATION_LOGGERS: Final = frozenset({"eyecite", "reporters_db", "courts_db"})
_EXTRACTION_LOCK = Lock()

type CaseExtractor = Callable[[str], tuple[tuple[contract.ExactObject, str | None], ...]]


@dataclasses.dataclass(frozen=True, slots=True)
class TurnObject:
    """An exact object in message order with its optional indexed spelling."""

    index: int
    exact: contract.ExactObject = dataclasses.field(repr=False)
    indexed_spelling: str | None = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True, slots=True)
class ObjectSearch:
    """One search against a lexical field for an exact object."""

    name: Literal["held", "citing", "matched"]
    field: Literal["caption", "cites", "text"]
    kind: Literal["phrase", "term"]
    values: tuple[str, ...] = dataclasses.field(repr=False)


def load_case_extractor() -> CaseExtractor | None:
    """Drop citation library diagnostics before loading the image-only adapter."""

    for name in sorted(CITATION_LOGGERS):
        logger = logging.getLogger(name)
        # Catch descendants' records here and keep them from the root and lastResort.
        logger.handlers[:] = [logging.NullHandler()]
        logger.propagate = False
        warnings.filterwarnings("ignore", module=rf"^{re.escape(name)}(?:\.|$)")

    try:
        from gideon.casecite.adapter import extract_indexed_case_cites
    except ImportError:
        return None
    return extract_indexed_case_cites


def _extract_before_deadline(
    message: str, adapter: CaseExtractor, deadline: float,
) -> tuple[TurnObject, ...]:
    remaining = deadline - monotonic()
    if remaining <= 0 or not _EXTRACTION_LOCK.acquire(timeout=remaining):
        raise TurnFailure(State.RETRIEVAL_TIMEOUT, "extraction")
    try:
        # A thread may get the lock just as its turn expires.
        if monotonic() >= deadline:
            raise TurnFailure(State.RETRIEVAL_TIMEOUT, "extraction")
        try:
            grammar = extract(message)
            cites = adapter(message)
            combined = contract.combine_extractions(grammar, tuple(obj for obj, _ in cites))
            invalid = (
                contract.span_violations(message, combined)
                or contract.key_violations(combined)
                or contract.ordering_violations(combined)
            )
            indexed = dict(cites)
        except Exception:  # noqa: BLE001 - refuse without exposing extraction diagnostics.
            raise TurnFailure(State.EXTRACTION_FAILED, "extraction") from None
        if invalid:
            raise TurnFailure(State.EXTRACTION_FAILED, "extraction")
        return tuple(TurnObject(index, obj, indexed.get(obj)) for index, obj in enumerate(combined))
    finally:
        _EXTRACTION_LOCK.release()


async def extract_objects(message: str, adapter: CaseExtractor | None) -> tuple[TurnObject, ...]:
    """Extract under one deadline, including the wait for the adapter lock."""

    if adapter is None:
        raise TurnFailure(State.EXTRACTION_UNAVAILABLE, "extraction")
    deadline = monotonic() + EXTRACTION_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout(deadline - monotonic()):
            return await asyncio.to_thread(_extract_before_deadline, message, adapter, deadline)
    except TimeoutError:
        raise TurnFailure(State.RETRIEVAL_TIMEOUT, "extraction") from None


def object_leg(
    obj: contract.ExactObject, indexed_spelling: str | None, message: str,
) -> tuple[ObjectSearch, ...]:
    """Describe the searches for an object in pin order."""

    typed = message[obj.start:obj.end]
    if obj.type == "case_cite":
        values: tuple[str, ...] = (typed,)
        if indexed_spelling is not None and indexed_spelling != typed:
            values = (typed, indexed_spelling)
        return (
            ObjectSearch("held", "caption", "phrase", values),
            ObjectSearch("citing", "cites", "term", values),
        )
    if obj.type in contract.KEYED_TYPES:
        assert obj.key is not None
        return (ObjectSearch("matched", "cites", "term", (obj.key,)),)
    if obj.type in ("bare_section", "bare_rule"):
        return (ObjectSearch("matched", "text", "phrase", (typed,)),)
    if obj.type == "docket":
        return (ObjectSearch("matched", "caption", "phrase", (typed,)),)
    return ()
