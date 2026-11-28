"""Build the content-free audit row for an admitted research turn."""

from collections.abc import Iterable, Mapping
from typing import Final, Literal, cast
from uuid import uuid4

from gideon.turn import auditlog, turnlog

TURN_KIND: Final = "research_turn"


def build_row(
    values: Mapping[str, object], *, sent_count: int, claim: Literal["user", "eval"],
    branch: str, user_id: str | None, kb_ids: Iterable[str],
) -> auditlog.AuditRow:
    """Project the kept values, bounded identifiers, and served branch into an audit row."""

    outcome = cast(turnlog.Outcome, values["outcome"])
    evidence = cast(list[dict[str, object]] | None, values["evidence"])
    timings = cast(Mapping[str, float | None], values["timings_ms"])
    return auditlog.AuditRow(
        run_id=uuid4(),
        kind=TURN_KIND,
        user_id=user_id,
        chat_id=cast(str | None, values["chat_id"]),
        kb_ids=tuple(sorted(kb_ids)),
        release=turnlog.product_version(),
        detail={
            "result": "allowed",
            "claim": claim,
            "branch": branch,
            "route": "retrieve" if values["stop_after"] == "retrieve" else "answer",
            "outcome": outcome,
            "failure_state": values["failure_state"],
            "message_id": values["message_id"],
            "plan_mode": values["plan_mode"],
            "corpus_lockfiles": values["corpus_lockfiles"],
            "returned": [{"chunk_id": hit["chunk_id"], "doc_id": hit["doc_id"]} for hit in (evidence or [])[:sent_count]],
            "support_level": values["support_level"],
            "gate_counts": None,
            "latency_ms": timings["total"],
        },
        log_reason=outcome,
    )
