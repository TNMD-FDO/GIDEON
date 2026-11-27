"""Resolve a research turn's claim into an admitted caller."""

import asyncio
from dataclasses import dataclass
from typing import Final, Literal

import httpx

from gideon.guardrail.writer import EVAL_SOURCE
from gideon.turn import frontend
from gideon.turn.generation import TURN_COLLECTION
from gideon.turn.states import FailureWord, State, TurnFailure

# exempt: a starting bound, corrected by the M1 reading.
ADMISSION_TIMEOUT_SECONDS: Final = 2.0
_COLLECTIONS: Final = frozenset({TURN_COLLECTION})

type DenialReason = Literal["no_session", "session_not_verified", "frontend_unavailable", "kb_not_readable"]
type KnowledgeFailures = tuple[tuple[str, frontend.ReplyWord], ...]

_REFUSALS: Final[dict[DenialReason, tuple[State, FailureWord | None]]] = {
    "no_session": (State.CALLER_NOT_ADMITTED, None),
    "session_not_verified": (State.SESSION_NOT_VERIFIED, "session_token"),
    "frontend_unavailable": (State.FRONTEND_UNAVAILABLE, "admission"),
    "kb_not_readable": (State.KB_ACCESS_DENIED, "kb_ids"),
}


@dataclass(frozen=True, slots=True)
class Floor:
    """The collection set and the knowledge-base grants for the turn."""

    collections: frozenset[str]
    granted: frozenset[str]


@dataclass(frozen=True, slots=True)
class Caller:
    """An admitted caller's kind and verified user id, when present."""

    kind: Literal["eval", "user"]
    user_id: str | None
    floor: Floor


def _is_reply_word(value: object) -> bool:
    return (isinstance(value, int) and 100 <= value <= 999) or (
        isinstance(value, str) and value in ("invalid", "none")
    )


@dataclass(frozen=True, slots=True)
class Denial:
    """The content-free facts of a refusal for a turn naming knowledge bases."""

    reason: DenialReason
    user_id: str | None
    session_reply: frontend.ReplyWord | None
    kb_ids: tuple[str, ...]
    failures: KnowledgeFailures

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or self.reason not in _REFUSALS:
            raise ValueError("Unknown denial reason.")
        if self.session_reply is not None and not _is_reply_word(self.session_reply):
            raise ValueError("Invalid session reply word.")
        if any(not _is_reply_word(reply) for _, reply in self.failures):
            raise ValueError("Invalid knowledge-base reply word.")


class CallerRefusal(TurnFailure):
    """A refusal during caller admission."""

    def __init__(self, state: State, word: FailureWord | None = None, *, denial: Denial | None = None) -> None:
        super().__init__(state, word)
        self.denial = denial


def _refusal(
    reason: DenialReason, kb_ids: tuple[str, ...], *, user_id: str | None = None,
    session_reply: frontend.ReplyWord | None = None, failures: KnowledgeFailures = (),
) -> CallerRefusal:
    state, word = _REFUSALS[reason]
    denial = Denial(reason, user_id, session_reply, kb_ids, failures) if kb_ids else None
    return CallerRefusal(state, word, denial=denial)


def _failed_reads(reads: list[tuple[str, asyncio.Task[tuple[bool, frontend.ReplyWord]]]]) -> KnowledgeFailures:
    failures: list[tuple[str, frontend.ReplyWord]] = []
    for kb_id, read in reads:
        permitted, reply = (False, "none") if read.cancelled() else read.result()
        if not permitted:
            failures.append((kb_id, reply))
    return tuple(failures)


async def admit_caller(
    claim: str, token: str | None, kb_ids: tuple[str, ...] | None, frontend_client: httpx.AsyncClient,
    *, timeout: float = ADMISSION_TIMEOUT_SECONDS,
) -> Caller:
    """Verify the session and re-check the named knowledge bases under one deadline."""
    named = kb_ids or ()
    if claim == EVAL_SOURCE and not named:
        return Caller("eval", None, Floor(_COLLECTIONS, frozenset()))
    if token is None:
        raise _refusal("no_session", named)
    if not frontend.session_token_shaped(token):
        raise _refusal("session_not_verified", named, session_reply="invalid")

    user_id: str | None = None
    reads: list[tuple[str, asyncio.Task[tuple[bool, frontend.ReplyWord]]]] = []
    deadline = asyncio.timeout(timeout)

    # A read that returns after the deadline fired (its cancel swallowed below the
    # adapter) is counted as no reply, as a cancelled one is.
    async def check_knowledge(kb_id: str) -> tuple[bool, frontend.ReplyWord]:
        verdict = await frontend.read_knowledge(frontend_client, token, kb_id)
        return (False, "none") if deadline.expired() else verdict

    try:
        async with deadline:
            session_id, reply = await frontend.read_session(frontend_client, token)
            if deadline.expired():
                raise TimeoutError
            user_id = session_id
            if user_id is None:
                session_reason: DenialReason = "frontend_unavailable" if frontend.unavailable(reply) else "session_not_verified"
                raise _refusal(session_reason, named, session_reply=reply)
            async with asyncio.TaskGroup() as checks:
                reads = [(kb_id, checks.create_task(check_knowledge(kb_id))) for kb_id in named]
    except TimeoutError:
        raise _refusal(
            "frontend_unavailable", named, user_id=user_id, session_reply="none" if user_id is None else None,
            failures=_failed_reads(reads),
        ) from None

    failures = _failed_reads(reads)
    if failures:
        knowledge_reason: DenialReason = (
            "frontend_unavailable" if any(frontend.unavailable(reply) for _, reply in failures) else "kb_not_readable"
        )
        raise _refusal(knowledge_reason, named, user_id=user_id, failures=failures)
    # The claim names the kind; the session gates a knowledge base for the eval identity too.
    return Caller("eval" if claim == EVAL_SOURCE else "user", user_id, Floor(_COLLECTIONS, frozenset(named)))
