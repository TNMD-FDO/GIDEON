"""title: GIDEON branch gate
version: 1
description: Refuses user requests that address a model row without a preset branch.
"""

# The branch gate (ADR-0045 (d), docs/frontend-contract.md §2 row 8): an
# inlet-only global Filter refusing a user-role request whose model entry is
# not a preset, so the hidden base model answers no seat while admins and the
# eval identity pass (slice-1 ticket 43, general-turn ticket 04).  It is the
# one GIDEON Function the cutover left in the frontend, because nothing else
# can tell the base from General: a preset's turn reaches the connection under
# its base's id, and the base row keeps its public-read grant.  It depends on the Filter inlet and the
# model entry's info.base_model_id alone, reads no message, and keeps no state.
# This file runs inside the frontend's container and is imported by path in
# the unit tests, so it imports the standard library only, defines no Valves
# (nothing is tunable, and a Valves class would give it a priority), no toggle
# (a user could switch a toggleable Filter off), carries no requirements line
# (a pip install at load), and never contains the four import prefixes the
# frontend's rewriter replaces over the whole file — the word "from" followed
# by utils, apps, main, or config (docs/research/owui-filter-function.md
# §4.2).  The inlet order: the pinned frontend sorts a request's Filters by
# (priority, id), a Filter without Valves taking priority 0.  This is the only
# global inlet, so no order question arises, and a raising inlet ends the chain
# uncaught, so a refused request reads this gate's refusal and never reaches
# the connection (the note's §3.3 and §4.5).
from collections.abc import Mapping

# The inlet's refusal for a user-role request that names no preset branch.
BRANCH_REFUSAL = (
    "GIDEON answers only through one of its branches. Start a new chat and ask General."
)
# The evaluation identity (render/owui.py's EVAL_IDENTITY; a test holds the
# two equal): the one user-role account whose API calls the inlet lets through.
EVAL_IDENTITY_EMAIL = "gideon-eval@gideon.invalid"


class BranchRefusal(Exception):
    """The fixed refusal for a user request outside a preset branch."""


def _is_preset(model_entry: object) -> bool:
    """Return whether a model entry carries a non-empty base-model id."""

    # The entry is the process cache's discovered model; ``info`` is the record
    # merged onto it (with ``params`` deleted). A discovered model without a
    # record has no ``info`` in GIDEON's environment, or only ``meta`` if the
    # frontend's dormant default-metadata merge is enabled; the OpenAI router
    # swaps a preset for its base only after the Filters have run. This rule
    # therefore reads only ``info.base_model_id`` and never ``id`` or ``name``.
    if not isinstance(model_entry, Mapping):
        return False
    info = model_entry.get("info")
    if not isinstance(info, Mapping):
        return False
    base_model_id = info.get("base_model_id")
    return isinstance(base_model_id, str) and bool(base_model_id)


class Filter:
    """Global branch gate Filter for Open WebUI."""

    def inlet(
        self,
        body: object,
        __user__: Mapping[str, object] | None = None,
        __model__: Mapping[str, object] | None = None,
    ) -> object:
        # An unusable user fails closed with the one refusal this gate owns.
        if not isinstance(__user__, Mapping):
            raise BranchRefusal(BRANCH_REFUSAL)
        role = __user__.get("role")
        email = __user__.get("email")
        if role == "admin" or email == EVAL_IDENTITY_EMAIL:
            return body
        if role == "user" and not _is_preset(__model__):
            raise BranchRefusal(BRANCH_REFUSAL)
        return body
