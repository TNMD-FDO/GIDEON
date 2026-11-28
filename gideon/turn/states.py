"""Fixed failure states and content-free error replies for a research turn."""

from enum import StrEnum
from http import HTTPStatus
from typing import Final, Literal, get_args

from gideon.api.errors import error_body


class State(StrEnum):
    INVALID_REQUEST = "invalid_request"
    EVAL_ONLY_FIELD = "eval_only_field"
    CALLER_NOT_ADMITTED = "caller_not_admitted"
    SESSION_NOT_VERIFIED = "session_not_verified"
    FRONTEND_UNAVAILABLE = "frontend_unavailable"
    KB_ACCESS_DENIED = "kb_access_denied"
    OVERRIDE_NOT_ALLOWED = "override_not_allowed"
    OVERRIDE_INVALID = "override_invalid"
    STAGE_NOT_BUILT = "stage_not_built"
    KB_IDS_NOT_BUILT = "kb_ids_not_built"
    NO_LIVE_GENERATION = "no_live_generation"
    AMBIGUOUS_GENERATION = "ambiguous_generation"
    GENERATION_NOT_READABLE = "generation_not_readable"
    RECORD_UNAVAILABLE = "record_unavailable"
    LOCKFILE_OUTSIDE_RELEASE = "lockfile_outside_release"
    EXTRACTION_UNAVAILABLE = "extraction_unavailable"
    EXTRACTION_FAILED = "extraction_failed"
    EMBEDDER_UNAVAILABLE = "embedder_unavailable"
    EMBEDDER_OUTSIDE_SPACE = "embedder_outside_space"
    MESSAGE_OUTSIDE_WINDOW = "message_outside_window"
    STORE_UNAVAILABLE = "store_unavailable"
    STORE_REPLY_INVALID = "store_reply_invalid"
    RETRIEVAL_TIMEOUT = "retrieval_timeout"
    RERANKER_UNAVAILABLE = "reranker_unavailable"
    RERANKER_REPLY_INVALID = "reranker_reply_invalid"
    RERANK_QUEUE_TIMEOUT = "rerank_queue_timeout"
    PAIR_OUTSIDE_WINDOW = "pair_outside_window"
    GENERATOR_UNAVAILABLE = "generator_unavailable"
    GENERATOR_OUTSIDE_RELEASE = "generator_outside_release"
    GENERATOR_REPLY_INVALID = "generator_reply_invalid"
    GENERATION_EMPTY = "generation_empty"
    INTERNAL_ERROR = "internal_error"


StageWord = Literal["admission", "record", "extraction", "embedding", "fusion", "rerank", "retrieve", "passages", "generate"]
LegWord = Literal["dense", "lexical", "objects", "controlling"]
ReasonWord = Literal["generation", "repository", "width", "listing"]
RequestWord = Literal[
    "message", "message_id", "history", "kb_ids", "session_token", "stop_after", "evidence", "overrides", "generation",
    "leg_limit", "pool_cap", "reservations.exact_object", "reservations.controlling",
    "rrf.k", "collapse", "boilerplate_weight", "query_plan",
    "rerank.stage1", "rerank.stage2", "stage2_depth", "rerank.semaphore", "rerank.timeout_s", "final_k",
    "hierarchy.mode", "hierarchy.weights", "final_reserved_controlling", "per_document_cap", "per_hit_chunks",
    "context_budget_tokens", "abstention.floor", "abstention.thin_below",
]
type FailureWord = StageWord | LegWord | ReasonWord | RequestWord

_WORDS: Final = frozenset((*get_args(StageWord), *get_args(LegWord), *get_args(ReasonWord), *get_args(RequestWord)))
_ERRORS: Final[dict[State, tuple[HTTPStatus, str]]] = {
    State.INVALID_REQUEST: (HTTPStatus.BAD_REQUEST, "The turn request is invalid."),
    State.EVAL_ONLY_FIELD: (HTTPStatus.FORBIDDEN, "This field is reserved for evaluation."),
    State.CALLER_NOT_ADMITTED: (HTTPStatus.FORBIDDEN, "The caller is not admitted to this turn."),
    State.SESSION_NOT_VERIFIED: (HTTPStatus.UNAUTHORIZED, "The session could not be verified."),
    State.FRONTEND_UNAVAILABLE: (HTTPStatus.SERVICE_UNAVAILABLE, "The frontend is unavailable."),
    State.KB_ACCESS_DENIED: (HTTPStatus.FORBIDDEN, "Knowledge-base access was denied."),
    State.OVERRIDE_NOT_ALLOWED: (HTTPStatus.BAD_REQUEST, "The retrieval override is not allowed."),
    State.OVERRIDE_INVALID: (HTTPStatus.BAD_REQUEST, "The retrieval override value is invalid."),
    State.STAGE_NOT_BUILT: (HTTPStatus.NOT_IMPLEMENTED, "The requested turn stage is unavailable."),
    State.KB_IDS_NOT_BUILT: (HTTPStatus.NOT_IMPLEMENTED, "Knowledge-base retrieval is unavailable."),
    State.NO_LIVE_GENERATION: (HTTPStatus.SERVICE_UNAVAILABLE, "The collection has no live generation."),
    State.AMBIGUOUS_GENERATION: (HTTPStatus.SERVICE_UNAVAILABLE, "The collection has multiple live generations."),
    State.GENERATION_NOT_READABLE: (HTTPStatus.CONFLICT, "The named generation is not built or live."),
    State.RECORD_UNAVAILABLE: (HTTPStatus.SERVICE_UNAVAILABLE, "The generation record is unavailable."),
    State.LOCKFILE_OUTSIDE_RELEASE: (HTTPStatus.SERVICE_UNAVAILABLE, "The generation's corpus lockfile is outside the release."),
    State.EXTRACTION_UNAVAILABLE: (HTTPStatus.SERVICE_UNAVAILABLE, "Exact-object extraction is unavailable."),
    State.EXTRACTION_FAILED: (HTTPStatus.INTERNAL_SERVER_ERROR, "Exact-object extraction failed."),
    State.EMBEDDER_UNAVAILABLE: (HTTPStatus.SERVICE_UNAVAILABLE, "The embedder is unavailable."),
    State.EMBEDDER_OUTSIDE_SPACE: (HTTPStatus.SERVICE_UNAVAILABLE, "The embedder is outside the retrieval space."),
    State.MESSAGE_OUTSIDE_WINDOW: (HTTPStatus.BAD_REQUEST, "The message exceeds the embedding context window."),
    State.STORE_UNAVAILABLE: (HTTPStatus.SERVICE_UNAVAILABLE, "The retrieval store is unavailable."),
    State.STORE_REPLY_INVALID: (HTTPStatus.BAD_GATEWAY, "The retrieval store returned an invalid reply."),
    State.RETRIEVAL_TIMEOUT: (HTTPStatus.GATEWAY_TIMEOUT, "The retrieval deadline expired."),
    State.RERANKER_UNAVAILABLE: (HTTPStatus.SERVICE_UNAVAILABLE, "The reranker is unavailable."),
    State.RERANKER_REPLY_INVALID: (HTTPStatus.BAD_GATEWAY, "The reranker returned an invalid reply."),
    State.RERANK_QUEUE_TIMEOUT: (HTTPStatus.SERVICE_UNAVAILABLE, "The rerank queue deadline expired."),
    State.PAIR_OUTSIDE_WINDOW: (HTTPStatus.BAD_REQUEST, "The message and a passage exceed the reranker context window."),
    State.GENERATOR_UNAVAILABLE: (HTTPStatus.SERVICE_UNAVAILABLE, "The generator is unavailable."),
    State.GENERATOR_OUTSIDE_RELEASE: (HTTPStatus.SERVICE_UNAVAILABLE, "The generator is outside the release."),
    State.GENERATOR_REPLY_INVALID: (HTTPStatus.BAD_GATEWAY, "The generator returned an invalid reply."),
    State.GENERATION_EMPTY: (HTTPStatus.BAD_GATEWAY, "The generator returned no answer text."),
    State.INTERNAL_ERROR: (HTTPStatus.INTERNAL_SERVER_ERROR, "The turn could not be completed."),
}


class TurnFailure(Exception):
    """A turn failure carrying a fixed state and an optional vocabulary word."""

    def __init__(self, state: State, word: FailureWord | None = None) -> None:
        super().__init__(state.value)
        self.state = state
        self.word = word if word in _WORDS else None

    @property
    def status(self) -> HTTPStatus:
        return _ERRORS[self.state][0]

    def to_body(self) -> dict[str, dict[str, object]]:
        status, message = _ERRORS[self.state]
        error_type = "invalid_request_error" if status < HTTPStatus.INTERNAL_SERVER_ERROR else "server_error"
        return error_body(message, error_type=error_type, code=self.state.value, param=self.word)
