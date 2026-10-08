"""The GPU record: host state binding each models.lock GPU position to a card.

The first render that finds no record writes the cards in the order
``nvidia-smi -L`` reports them; later renders and preflight read the record
instead of the order, so a card added or enumerated differently moves no
model server.
Host state, never a site key, and outside every backup set like the markers.
"""

from pathlib import Path
from typing import Final

import yaml  # type: ignore[import-untyped]

from gideon.host import report
from gideon.host.report import Problem
from gideon.host.sysio import Host

GPU_RECORD_PATH: Final = Path("/etc/gideon/gpus.yaml")
_RECORD_HEADER: Final = (
    "# Written by gideon render on first use from nvidia-smi -L's order.\n"
    "# Each position is a models.lock gpu: index.\n"
    "# After a card change, remove this file as root and re-run render to re-record.\n"
)
MISSING_CARD_PROBLEM: Final = f"the GPU record {GPU_RECORD_PATH} names card(s) nvidia-smi -L no longer reports"
MALFORMED_RECORD_PROBLEM: Final = f"the GPU record {GPU_RECORD_PATH} is malformed"
CREATE_RECORD_FIX: Final = (
    f"Create {GPU_RECORD_PATH} as root with mode 0644 and a gpus: list of "
    "today's card UUIDs in lock-position order, then re-run render."
)


def re_record_fix() -> str:
    """Name the re-record command in this run's command form."""

    return (
        f"Remove {GPU_RECORD_PATH} as root, then run "
        f"{report.command('render --diff')} to record today's cards and inspect the diff."
    )


def read(host: Host) -> tuple[str, ...] | Problem | None:
    """Read the card order, or return None when no record exists."""

    try:
        text = host.read_text(GPU_RECORD_PATH)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        return Problem(f"cannot read the GPU record {GPU_RECORD_PATH}: {exc}", re_record_fix())
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return Problem(f"{MALFORMED_RECORD_PROBLEM}: {exc}", re_record_fix())
    if not isinstance(document, dict) or set(document) != {"gpus"}:
        return Problem(MALFORMED_RECORD_PROBLEM, re_record_fix())
    cards = document["gpus"]
    if (
        not isinstance(cards, list)
        or not cards
        or any(not isinstance(card, str) or not card.strip() for card in cards)
        or len(set(cards)) != len(cards)
    ):
        return Problem(MALFORMED_RECORD_PROBLEM, re_record_fix())
    return tuple(cards)


def write(host: Host, cards: tuple[str, ...]) -> Problem | None:
    """Write the first card order absent-only, as a root-owned 0644 file."""

    try:
        if host.exists(GPU_RECORD_PATH):
            return None
        host.mkdir(GPU_RECORD_PATH.parent, mode=0o755, parents=True, exist_ok=True)
        text = _RECORD_HEADER + "gpus:\n" + "".join(f"  - {card}\n" for card in cards)
        host.write_text(GPU_RECORD_PATH, text, mode=0o644)
    except OSError as exc:
        return Problem(f"cannot write the GPU record {GPU_RECORD_PATH}: {exc}", CREATE_RECORD_FIX)
    return None


def resolve(recorded: tuple[str, ...] | None, present: tuple[str, ...]) -> tuple[str, ...] | Problem:
    """Resolve lock positions from a record, or today's order when unrecorded."""

    if recorded is None:
        return present
    present_cards = set(present)
    missing = [
        f"GPU index {position} UUID {card}"
        for position, card in enumerate(recorded)
        if card not in present_cards
    ]
    if missing:
        return Problem(f"{MISSING_CARD_PROBLEM}: {', '.join(missing)}", re_record_fix())
    return recorded


def bind(host: Host, present: tuple[str, ...]) -> tuple[str, ...] | Problem:
    """Resolve lock positions and record today's order on first render."""

    recorded = read(host)
    if isinstance(recorded, Problem):
        return recorded
    resolved = resolve(recorded, present)
    if recorded is None and present:
        problem = write(host, present)
        if problem is not None:
            return problem
    return resolved
