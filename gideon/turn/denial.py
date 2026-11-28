"""Build content-free audit rows for conflicts-wall denials."""

from typing import Final
from uuid import uuid4

from gideon.guardrail import writer
from gideon.turn import auditlog, turnlog
from gideon.turn.caller import Denial

DENIAL_KIND: Final = "conflicts_wall_denial"


def build_row(denial: Denial, claim: str, chat_id: str | None) -> auditlog.AuditRow:
    """Keep the claim, bounded identifiers, and reply words for one incident."""
    detail: dict[str, object] = {
        "reason": denial.reason,
        "result": "denied",
        "claim": claim,
        "failures": [{"kb_id": kb_id, "reply": reply} for kb_id, reply in denial.failures],
    }
    if denial.session_reply is not None:
        detail["session_reply"] = denial.session_reply
    return auditlog.AuditRow(
        run_id=uuid4(),
        kind=DENIAL_KIND,
        user_id=denial.user_id,
        chat_id=writer.bounded_id(chat_id),
        kb_ids=denial.kb_ids,
        release=turnlog.product_version(),
        detail=detail,
        log_reason=denial.reason,
    )
