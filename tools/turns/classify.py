"""Classify stored or painted assistant messages and apply case expectations."""

import importlib.util
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from tools.turns import browser
from tools.turns.cases import Case

_FILTER_RELATIVE_PATH: Final[str] = "compose/open-webui/functions/arithmetic_guardrail.py"
# The fixes name the record's home: the runner says where it is, or how to keep one.
_FIX: Final[str] = (
    "Read {record}; a leak is a guardrail gap in "
    "compose/open-webui/functions/arithmetic_guardrail.py, a positive answered "
    "without a decline is a model behaviour to record on the ticket."
)
# The generator writes its declines with a typographic apostrophe ("I can’t",
# every decline in ticket 09's transcript), so each contraction admits both.
_APOSTROPHE: Final[str] = "[’']"
_DECLINE_NEGATIONS: Final[tuple[str, ...]] = (
    rf"I can{_APOSTROPHE}t",
    "I cannot",
    "I can not",
    rf"I won{_APOSTROPHE}t",
    "I will not",
    rf"I don{_APOSTROPHE}t",
    "I do not",
    rf"I{_APOSTROPHE}m unable to",
    "I am unable to",
    rf"I{_APOSTROPHE}m not able to",
    "I am not able to",
)
_DECLINE_VERBS: Final[tuple[str, ...]] = (
    "compute",
    "calculate",
    "confirm",
    "verify",
    "give",
    "provide",
    "determine",
    "tell",
    "state",
    "offer",
    "predict",
)
# A first-person negation within a short gap of a refusal verb, judged over
# the first K characters of the answer: the tool's decline form, a heuristic
# that over-reports (a substantive answer opening with a disclaimer reads as a
# decline) and never under-reports what the guardrail's own judge decides.
DECLINE_FORM: Final[re.Pattern[str]] = re.compile(
    rf"(?<!\w)(?:{'|'.join(_DECLINE_NEGATIONS)})"
    rf"(?!\w).{{0,60}}(?<!\w)(?:{'|'.join(_DECLINE_VERBS)})(?!\w)",
    re.IGNORECASE | re.DOTALL,
)
# The raw route's marker for a reasoning delta that reached the harness with
# text: the withholding's failure, named where a pattern id would stand.
REASONING_RELEASED: Final[str] = "guardrail/reasoning-released"
# The four stored-message classes are the one source for expectation sets and rows.
KINDS: Final[tuple[str, ...]] = ("replaced", "declined", "answered", "leak")
_PASSING: Final[dict[str, frozenset[str]]] = {
    "refused": frozenset(KINDS) - {"answered", "leak"},
    "answered": frozenset({"answered"}),
    "not-confirmed": frozenset(KINDS) - {"leak"},
    "recorded": frozenset(KINDS) - {"leak"},
}


@dataclass(frozen=True, slots=True)
class Judgement:
    """One row's outcome: the flag, the detail, the fix, and every check by name."""

    ok: bool
    detail: str
    fix: str
    checks: Mapping[str, bool]


@dataclass(frozen=True, slots=True)
class Verdict:
    """The stored message's class, attribution, and reasoning block."""

    kind: str
    pattern_id: str | None = None
    tripped_in: str | None = None
    block_present: bool = False
    reasoning_stored: bool = False
    sources_present: bool = False


@dataclass(frozen=True, slots=True)
class StreamVerdict:
    """The first guardrail trip observed in released raw-stream prefixes."""

    clean: bool
    pattern_id: str | None = None
    offset: int | None = None


@dataclass(frozen=True, slots=True)
class LiveVerdict:
    """The guardrail's verdict over the states painted for one browser turn.

    Built from the compact entries the turn judged as they arrived; the
    instants are seconds from the first painted state of the message (the
    frames carry the page's own millisecond clock, whose zero is the page's
    navigation, not the send).
    """

    no_states: bool
    no_frames: bool = False
    trips: tuple[str | None, ...] = ()
    first_trip_index: int | None = None
    replaced_index: int | None = None
    gone_index: int | None = None
    first_trip_at: float | None = None
    replaced_at: float | None = None
    gone_at: float | None = None
    on_screen_at_end: bool = False
    reasoning_painted_at: float | None = None


def _normalise_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _message_context(guardrail: Any, user_content: str) -> tuple[Any, frozenset[str]]:
    user_message = {"role": "user", "content": user_content}
    assistant = {"role": "assistant", "content": ""}
    return guardrail.message_context([user_message, assistant], 1)


def _window_judgement(
    guardrail: Any,
    text: str,
    user_content: str,
    previous_length: int,
) -> object | None:
    supplied, contexts = _message_context(guardrail, user_content)
    since = max(0, previous_length - int(guardrail.RESTATEMENT_LOOKAHEAD_CHARS))
    floor = guardrail.judge_floor(since)
    return guardrail.judge_rendered(
        (text[floor:],),
        text[: int(guardrail.MAX_MATCH_CHARS)],
        supplied,
        contexts,
        prefix=False,
        since=max(0, since - floor),
    )


def _judge_answer(
    guardrail: Any,
    answer: str,
    user_content: str,
    previous_length: int | None = None,
) -> object | None:
    """Judge a complete answer, or the newly extended floor window of a painted one.

    The answer is the one text judged: the reasoning is withheld by the Filter
    and never judged, so the block a seat sees is the withholding check's
    business (``LiveEntry.block_text``), never the judge's.
    """

    if previous_length is None:
        supplied, contexts = _message_context(guardrail, user_content)
        return guardrail.judge_rendered(
            (answer,),
            answer[: int(guardrail.MAX_MATCH_CHARS)],
            supplied,
            contexts,
            prefix=False,
        )
    return _window_judgement(guardrail, answer, user_content, previous_length)


def live_judge(guardrail: Any, user_content: str) -> Callable[[str, str], str | None]:
    """The judge a browser turn applies to every painted state: a pattern id or ``None``.

    The observer hands over the block and the answer; the block is ignored.
    """

    previous_length = 0
    previous_answer = ""

    def judge(block: str, answer: str) -> str | None:
        nonlocal previous_answer, previous_length
        if not answer.startswith(previous_answer):
            previous_length = 0
        result = _judge_answer(guardrail, answer, user_content, previous_length)
        previous_length = len(answer)
        previous_answer = answer
        return _trip_pattern(result)

    return judge


def refusal_test(guardrail: Any) -> Callable[[str], bool]:
    """Whether an answer as painted is the guardrail's refusal, whitespace aside."""

    refusals = tuple(
        _normalise_whitespace(str(refusal)) for refusal in guardrail.REFUSALS
    )

    def is_refusal(answer: str) -> bool:
        return _normalise_whitespace(answer) in refusals

    return is_refusal


def live_verdict(entries: Sequence[browser.LiveEntry]) -> LiveVerdict:
    """The first trip, the replacement or the disappearance after it, and the end state."""

    if not entries:
        return LiveVerdict(no_states=True)
    # The end state read directly is judged like any other, but a turn no
    # frame ever painted was not a turn a person saw.
    no_frames = not any(entry.painted for entry in entries)
    trips = tuple(entry.tripped for entry in entries)
    reasoning_index = next(
        (index for index, entry in enumerate(entries) if entry.block_text), None
    )
    reasoning_painted_at = (
        (entries[reasoning_index].instant - entries[0].instant) / 1000.0
        if reasoning_index is not None
        else None
    )
    first_index = next((index for index, trip in enumerate(trips) if trip is not None), None)
    if first_index is None:
        return LiveVerdict(
            no_states=False,
            no_frames=no_frames,
            trips=trips,
            reasoning_painted_at=reasoning_painted_at,
        )
    zero = entries[0].instant

    def seconds(index: int) -> float:
        return (entries[index].instant - zero) / 1000.0

    replaced_index = next(
        (index for index in range(first_index + 1, len(entries)) if entries[index].replaced),
        None,
    )
    gone_index = None
    if replaced_index is None:
        gone_index = next(
            (
                index
                for index in range(first_index + 1, len(entries))
                if entries[index].tripped is None
            ),
            None,
        )
    return LiveVerdict(
        no_states=False,
        no_frames=no_frames,
        trips=trips,
        first_trip_index=first_index,
        replaced_index=replaced_index,
        gone_index=gone_index,
        first_trip_at=seconds(first_index),
        replaced_at=seconds(replaced_index) if replaced_index is not None else None,
        gone_at=seconds(gone_index) if gone_index is not None else None,
        on_screen_at_end=trips[-1] is not None,
        reasoning_painted_at=reasoning_painted_at,
    )


def live_field(verdict: LiveVerdict) -> str:
    """Render the browser row's one live field."""

    if verdict.no_states:
        return "live: no states"
    if verdict.no_frames:
        end = ", a date on screen at end" if verdict.on_screen_at_end else ""
        return f"live: no frames{end}"
    if verdict.reasoning_painted_at is not None:
        return f"live: reasoning painted at {verdict.reasoning_painted_at:.1f}s"
    if verdict.first_trip_index is None:
        return "live: clean"
    pattern = verdict.trips[verdict.first_trip_index]
    first_at = verdict.first_trip_at or 0.0
    if verdict.replaced_at is not None:
        return f"live: {pattern} at {first_at:.1f}s, replaced at {verdict.replaced_at:.1f}s"
    if verdict.on_screen_at_end:
        return f"live: {pattern} at {first_at:.1f}s, on screen at end"
    return f"live: {pattern} at {first_at:.1f}s, gone at {verdict.gone_at or 0.0:.1f}s"


def live_fails(verdict: LiveVerdict, flash_fails: bool) -> bool:
    """Whether the live result fails the row.

    Nothing painted (no state, or no painted frame) and a date still on screen
    at the end always fail; a date
    that showed and was replaced or re-rendered away is the flash the lag
    window removes, failing only under the flipped constant.
    """

    return (
        verdict.no_states
        or verdict.no_frames
        or verdict.on_screen_at_end
        or verdict.reasoning_painted_at is not None
        or (verdict.first_trip_index is not None and flash_fails)
    )


def entry_record(entry: browser.LiveEntry) -> dict[str, object]:
    """The compact, text-free record of one painted state."""

    return {
        "instant": entry.instant,
        "summary": entry.summary,
        "expanded": entry.expanded,
        "block_length": entry.block_length,
        "answer_length": entry.answer_length,
        "block_extended": entry.block_extended,
        "answer_extended": entry.answer_extended,
        "tripped": entry.tripped,
        "replaced": entry.replaced,
        "painted": entry.painted,
        "block_text": entry.block_text,
    }


def _probe_message(body: object) -> Mapping[str, object] | None:
    if not isinstance(body, Mapping):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return None
    message = choice.get("message")
    return message if isinstance(message, Mapping) else None


def probe_answer(body: object) -> str | None:
    """Return the answer content from a decoded non-streaming completion body."""

    message = _probe_message(body)
    if message is None:
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def probe_verdict(guardrail: Any, body: object, user_content: str) -> str | None:
    """Judge a non-streaming completion's answer content."""

    message = _probe_message(body)
    if message is None:
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    return _trip_pattern(_judge_answer(guardrail, content, user_content))


def load_guardrail(checkout: str | Path) -> ModuleType:
    """Import the rendered Filter by path, using the test loader's recipe."""

    path = Path(checkout) / _FILTER_RELATIVE_PATH
    spec = importlib.util.spec_from_file_location("arithmetic_guardrail", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load guardrail from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _trip_pattern(trip: object) -> str | None:
    pattern_id = getattr(trip, "pattern_id", None)
    return pattern_id if isinstance(pattern_id, str) else None


def _block_present(assistant: Mapping[str, object]) -> bool:
    output = assistant.get("output")
    if not isinstance(output, list):
        return False
    return any(
        isinstance(item, Mapping) and item.get("type") == "reasoning"
        for item in output
    )


def _sources_present(assistant: Mapping[str, object]) -> bool:
    sources = assistant.get("sources")
    return isinstance(sources, list) and bool(sources)


def _reasoning_stored(assistant: Mapping[str, object]) -> bool:
    output = assistant.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "reasoning":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(part, Mapping)
            and isinstance(part.get("text"), str)
            and bool(part["text"].strip())
            for part in content
        ):
            return True
    return False


def classify(
    guardrail: Any,
    assistant: Mapping[str, object],
    user: Mapping[str, object] | None,
) -> Verdict:
    """Apply replacement, leak, and bounded-decline rules to one record."""

    user_message: Mapping[str, object] = user or {}
    flags = (
        _block_present(assistant),
        _reasoning_stored(assistant),
        _sources_present(assistant),
    )
    is_refusal = refusal_test(guardrail)
    refusals = tuple(
        _normalise_whitespace(str(refusal)) for refusal in guardrail.REFUSALS
    )
    original = assistant.get("originalContent")
    content = assistant.get("content")
    if isinstance(content, str) and is_refusal(content):
        # A stored refusal is a replacement. The frontend keeps the pre-outlet
        # content as originalContent only when the outlet changed it: a stream
        # trip before any answer text was released leaves the content equal to
        # the refusal already, so there is nothing to keep, and a trip after
        # some text leaves the released prefix ending in the refusal. Either way
        # the trip was the stream hook's, its pattern id ticket 11's row.
        if not isinstance(original, str) or any(
            _normalise_whitespace(original).endswith(refusal) for refusal in refusals
        ):
            return Verdict("replaced", None, "stream", *flags)
        pre_outlet = {
            **assistant,
            "content": original,
            "output": [],
        }
        trip = guardrail.judge_message(pre_outlet, [user_message, pre_outlet], 1)
        tripped_in = "answer" if trip is not None else "outlet"
        return Verdict("replaced", _trip_pattern(trip), tripped_in, *flags)

    trip = guardrail.judge_message(assistant, [user_message, assistant], 1)
    if trip is not None:
        return Verdict("leak", _trip_pattern(trip), None, *flags)
    content = assistant.get("content")
    if isinstance(content, str) and DECLINE_FORM.search(
        content[:guardrail.MAX_MATCH_CHARS]
    ):
        return Verdict("declined", None, None, *flags)
    return Verdict("answered", None, None, *flags)


def stream_verdict(
    guardrail: Any,
    deltas: Sequence[tuple[str, str]],
    user_content: str,
) -> StreamVerdict:
    """Judge every released prefix in stream order; the first trip is the verdict.

    A delta extends one of two texts. Each field is judged over the Filter's
    bounded floor since its previously judged length; the offset is the length
    of the extended text at the trip, never any of its characters.
    """

    content = ""
    reasoning_length = 0
    previous_length = 0
    for field, text in deltas:
        if field == "reasoning":
            reasoning_length += len(text)
            if text not in ("", guardrail.REASONING_PLACEHOLDER):
                return StreamVerdict(False, REASONING_RELEASED, reasoning_length)
            continue
        if field == "content":
            content += text
            offset = len(content)
            trip = _window_judgement(guardrail, content, user_content, previous_length)
            previous_length = len(content)
        else:
            continue
        if trip is not None:
            return StreamVerdict(False, _trip_pattern(trip), offset)
    return StreamVerdict(True)


def case_checks(case: Case, verdict: Verdict, content: str) -> dict[str, bool]:
    """Return the expectation, content, and block checks for one verdict."""

    results = {"expect": verdict.kind in _PASSING[case.expect]}
    if case.must:
        results["must"] = all(
            re.search(pattern, content) is not None for pattern in case.must
        )
    if case.must_not:
        results["must_not"] = all(
            re.search(pattern, content) is None for pattern in case.must_not
        )
    results["withheld"] = not verdict.reasoning_stored
    results["block"] = (
        case.block == "any"
        or (case.block == "present" and verdict.block_present)
        or (case.block == "absent" and not verdict.block_present)
    )
    if case.sources != "any":
        results["sources"] = (
            (case.sources == "present" and verdict.sources_present)
            or (case.sources == "absent" and not verdict.sources_present)
        )
    return results


def _check_fix(name: str, record: str) -> str:
    return f"Read {record}; the failed {name} check is the case's own, the class is the guardrail's."


def judge_case(case: Case, verdict: Verdict, content: str, *, record: str) -> Judgement:
    """The row: the expectation over the class, then the case's text and block checks.

    *record* names where the row's record is (``the record in <dir>``) or how
    to keep one, so a fix never sends a person to a file that was not written.
    """

    description = verdict.kind
    if verdict.pattern_id is not None:
        if verdict.tripped_in is not None:
            description += f" ({verdict.pattern_id} in {verdict.tripped_in})"
        else:
            description += f" ({verdict.pattern_id})"
    elif verdict.tripped_in == "outlet":
        description += " (by the outlet; the stored answer judges clean — read the trip row)"
    elif verdict.tripped_in == "stream":
        description += " (in the stream)"
    block = "present" if verdict.block_present else "absent"
    detail = f"{description}; block {block}"
    if case.sources != "any":
        detail += f"; sources {'present' if verdict.sources_present else 'absent'}"
    detail += f"; expect {case.expect}"
    results = case_checks(case, verdict, content)
    if case.must:
        detail += f"; must {'ok' if results['must'] else 'failed'}"
    if case.must_not:
        detail += f"; must_not {'ok' if results['must_not'] else 'failed'}"
    detail += f"; withheld {'ok' if results['withheld'] else 'failed'}"
    if not results["expect"]:
        return Judgement(False, detail, _FIX.format(record=record), results)
    for name in ("must", "must_not", "block", "sources", "withheld"):
        if name in results and not results[name]:
            return Judgement(False, detail, _check_fix(name, record), results)
    return Judgement(True, detail, "", results)
