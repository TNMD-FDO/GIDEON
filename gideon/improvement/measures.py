"""Read committed improvement measures through the host I/O seam."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.evaluation import evalset
from gideon.host.report import Problem
from gideon.improvement import triggers
from gideon.improvement.sections import Context

type Figures = Mapping[str, tuple[int | float, ...]]
type Reader = Callable[[Context], Figures | Problem]


@dataclass(frozen=True, slots=True)
class MeasureReader:
    """A measure's declared figure names and the function that reads them."""

    figures: tuple[str, ...]
    read: Reader


_RESTORE_FIX: Final[str] = "Restore the eval set from the release checkout, then retry."
_EVAL_SET_FIGURES: Final[tuple[str, ...]] = triggers.MEASURES["eval-set"]
_HELD_OUT_RELATIVE: Final[Path] = Path("slices", "judgments-held-out")


def _eval_set(context: Context) -> Figures | Problem:
    """Count primary judged queries and distinct held-out ids."""

    from gideon.evaluation import judgments

    set_root = context.checkout_root / evalset.SET_ROOT
    judgments_path = set_root / judgments.JUDGMENTS_PATH
    judged_queries = 0
    try:
        present = context.host.exists(judgments_path)
        contents = context.host.read_text(judgments_path) if present else ""
    except (OSError, UnicodeError):
        return Problem("judgments file could not be read", _RESTORE_FIX)
    if present:
        parsed = judgments.parse_bytes(contents.encode("utf-8"))
        if parsed.findings:
            return Problem("judgments file has parse findings", parsed.findings[0].fix)
        judged_queries = len(
            {
                located.record.query_id
                for located in parsed.records
                if located.record.assessment == "primary"
            }
        )

    held_out_dir = set_root / _HELD_OUT_RELATIVE
    try:
        if context.host.exists(held_out_dir):
            filenames = context.host.listdir(held_out_dir)
        else:
            filenames = []
        held_out_ids: set[str] = set()
        for filename in sorted(filenames):
            if not filename.endswith(".ids"):
                continue
            contents = context.host.read_text(held_out_dir / filename)
            held_out_ids.update(
                line.strip() for line in contents.splitlines() if line.strip()
            )
    except (OSError, UnicodeError):
        return Problem("held-out judgments slice could not be read", _RESTORE_FIX)

    return {
        "judged_queries": (judged_queries,),
        "held_out_ids": (len(held_out_ids),),
    }


READERS: Final[Mapping[triggers.TriggerMeasure, MeasureReader]] = {
    "eval-set": MeasureReader(_EVAL_SET_FIGURES, _eval_set),
}
