"""The guardrail's judge: the restatement and attribution tests, ``judge_text``,
the message readers, and the entries over a rendered message and a floor.

Built over ``grammar`` and ``families``; ``window`` imports from it, and
General's service, ``engine verify``, and the turn harness call it through the
package.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from gideon.guardrail.families import (
    AFFIRMATION_OPEN,
    FAMILIES,
    Family,
    Pattern,
    Trip,
)
from gideon.guardrail.grammar import (
    AFFIRMATION_NEAR,
    ATTRIBUTION_REACH_CHARS,
    EXCLUSION_REACH_CHARS,
    EXEMPTION_REACH_CHARS,
    MAX_MATCH_CHARS,
    RESTATEMENT_LOOKAHEAD_CHARS,
    RESTATEMENT_REACH_CHARS,
)

# The judge's default context: no figure supplied by the user for any family
# and no confirmation context matched — the stricter side.
NO_SUPPLIED: Mapping[str, frozenset[str]] = MappingProxyType({})


def rendered_texts(message: Mapping[str, object]) -> tuple[str, ...]:
    """Return the answer and non-reasoning output text parts as plain strings."""

    texts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        texts.append(content)
    output = message.get("output")
    if not isinstance(output, list):
        return tuple(texts)
    for item in output:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") != "message":
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    return tuple(texts)


def _is_restatement(
    text: str, match: re.Match[str], family: Family
) -> bool:
    """Whether a family match is governed by a restating construction."""

    matched = match.group(0)
    reverse = family.reverse_form.match(matched) is not None
    before = text[max(0, match.start() - RESTATEMENT_REACH_CHARS) : match.start()]
    after = text[match.end() : match.end() + RESTATEMENT_LOOKAHEAD_CHARS]
    window = before + matched + after
    span_start, span_end = len(before), len(before) + len(matched)
    # A question form carries its predicate by construction and governs even a
    # reverse-shaped span ("whether June 5, 2027 is your deadline").
    question_form, refusal_form, that_clause, from_date_form = family.constructions
    for form in question_form.finditer(window):
        if form.start() < span_end and form.end() > span_start:
            return True
    if AFFIRMATION_NEAR.search(before[-40:] + matched):
        return False
    if family.echo_form is not None:
        for form in family.echo_form.finditer(window):
            figure_start, figure_end = form.span("echo_figure")
            if span_start <= figure_start and figure_end <= span_end:
                return AFFIRMATION_NEAR.search(after[:30]) is None
    # A refusal verb's object reaches the date with no clause break; a
    # reverse-shaped span ("June 5, 2027 is the deadline") only through the
    # refusal's that-clause, since outside it the shape asserts.
    for form in refusal_form.finditer(window):
        figure_start, figure_end = form.span("refusal_figure")
        if (
            span_start <= figure_start
            and figure_end <= span_end
            and (not reverse or that_clause.match(form.group("object")) is not None)
        ):
            return AFFIRMATION_NEAR.search(after[:30]) is None
    if reverse:
        return False  # a reverse-form assertion: "June 5, 2027 is your deadline"
    # The refusal verb's object through a "from the <date> <noun>" phrase.
    for form in from_date_form.finditer(window):
        figure_start, figure_end = form.span("from_figure")
        if span_start <= figure_start and figure_end <= span_end:
            return AFFIRMATION_NEAR.search(after[:30]) is None
    return False


def _is_attributed(
    text: str, match: re.Match[str], pattern: Pattern, family: Family
) -> bool:
    """Whether a family's attribution form reaches this pattern's anchor."""

    if family.attribution_form is None:
        return False
    anchor = match.start()
    for name in pattern.anchor:
        span = match.span(name)
        if span != (-1, -1):
            anchor = span[0]
            break
    window = text[max(0, anchor - ATTRIBUTION_REACH_CHARS) : anchor + 1]
    return family.attribution_form.search(window) is not None


@dataclass(frozen=True, slots=True)
class Constraint:
    """A context that must remain together before a stream can release it."""

    start: int
    end: int


def judge_text(
    text: str,
    supplied: Mapping[str, frozenset[str]] = NO_SUPPLIED,
    contexts: frozenset[str] = frozenset(),
    *,
    prefix: bool = False,
    since: int = 0,
) -> Trip | tuple[Constraint, ...] | None:
    """Return the first family pattern matching one rendered text.

    A match whose every figure the user supplied is skipped only when it is
    a locally scoped answer restatement (``_is_restatement``).  Any figure the
    user did not supply, any reverse-form assertion, and any affirmation beside
    the user's figure trips. Context-gated patterns are skipped while their
    family is absent from ``contexts``.

    In prefix mode the text is still being generated and ``since`` is the
    length judged with full context at the last call: a hit ending at or
    before it is skipped; a hit ending within the restatement look-ahead of
    the end is undecided and skipped.  The result is then the first trip, or
    else the release constraints of the exempt hits: the hit and its
    look-ahead, which the stream never splits.  Those constraints also include
    rejected matches' exclusion tails and rate-form hits, so the stream never
    splits a hit from the context that decided it.
    """

    constraints: list[Constraint] = []
    for family in FAMILIES:
        family_supplied = supplied.get(family.name, frozenset())
        for pattern in family.patterns:
            if pattern.needs_context and family.name not in contexts:
                continue
            if any(name in contexts for name in pattern.yields_to):
                continue
            for match in pattern.regex.finditer(text):
                if pattern.exclusion is not None:
                    # A hit of the pattern's exclusion that covers the match's
                    # start — the lead or the count the match begins with —
                    # makes the match another family's (or nobody's): no
                    # trip.  When the hit reaches past the match, prefix mode
                    # holds the match to the hit's end, so no release boundary
                    # falls inside the context that rejected it (general-turn
                    # ticket 12).  A hit deeper inside the
                    # match (a second clause the deadline's greedy gap
                    # swallowed) rejects nothing, so the earlier family keeps
                    # its trip.
                    exclusion_start = max(0, match.start() - EXCLUSION_REACH_CHARS)
                    exclusion_end = min(len(text), match.end() + EXCLUSION_REACH_CHARS)
                    exclusion_hit = next(
                        (
                            exclusion
                            for exclusion in pattern.exclusion.finditer(
                                text[exclusion_start:exclusion_end]
                            )
                            if exclusion_start + exclusion.start()
                            <= match.start()
                            < exclusion_start + exclusion.end()
                        ),
                        None,
                    )
                    if exclusion_hit is not None:
                        if (
                            prefix
                            and exclusion_start + exclusion_hit.end() > match.end()
                        ):
                            constraints.append(
                                Constraint(
                                    match.start(),
                                    exclusion_start + exclusion_hit.end(),
                                )
                            )
                        continue
                if match.end() <= since:
                    continue
                if prefix and match.end() + RESTATEMENT_LOOKAHEAD_CHARS > len(text):
                    continue
                if _is_attributed(text, match, pattern, family):
                    # An authority's figure: no trip, no constraint, as an
                    # exclusion's hit rejects.
                    continue
                found = family.figures(match.group(0))
                if not found or not found <= family_supplied:
                    return Trip(family.name, pattern.pattern_id)
                if not _is_restatement(text, match, family):
                    return Trip(family.name, pattern.pattern_id)
                if prefix:
                    constraints.append(
                        Constraint(
                            match.start(), match.end() + RESTATEMENT_LOOKAHEAD_CHARS
                        )
                    )
            # The rate form's hits never trip: each holds a count to the rate
            # phrase that rejects it.  A hit whose phrase is still arriving
            # lies past the release point by the lag less the look-ahead, and
            # the next call's longer span joins it in the window's active set.
            if prefix and pattern.rate_form is not None:
                for hit in pattern.rate_form.finditer(text):
                    constraints.append(Constraint(hit.start(), hit.end()))
    return tuple(constraints) if prefix and constraints else None


def _last_assistant_position(messages: list[object]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "assistant":
            return index
    return None


def last_assistant_message(messages: list[object]) -> dict[str, object] | None:
    """Return the last mutable assistant message, if the body has one."""

    position = _last_assistant_position(messages)
    if position is None:
        return None
    message = messages[position]
    return message if isinstance(message, dict) else None


def preceding_user_message(
    messages: list[object], assistant_position: int
) -> dict[str, object] | None:
    """Return the nearest user message before the assistant message."""

    for index in range(assistant_position - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            return message
    return None


def message_context(
    messages: list[object], assistant_position: int
) -> tuple[Mapping[str, frozenset[str]], frozenset[str]]:
    """Read each family's supplied figures and confirmation context for one answer."""

    user_message = preceding_user_message(messages, assistant_position)
    user_content = user_message.get("content") if user_message is not None else None
    if not isinstance(user_content, str):
        user_content = ""
    supplied = {
        family.name: family.figures(user_content) for family in FAMILIES
    }
    contexts = {
        family.name
        for family in FAMILIES
        if family.confirmation is not None
        and user_content
        and family.confirmation[0].search(user_content)
    }
    for family in FAMILIES:
        if family.name in contexts:
            contexts.difference_update(family.confirmation_displaces)
    return supplied, frozenset(contexts)


def judge_rendered(
    texts: tuple[str, ...],
    answer: str,
    supplied: Mapping[str, frozenset[str]] = NO_SUPPLIED,
    contexts: frozenset[str] = frozenset(),
    *,
    prefix: bool = False,
    since: int = 0,
) -> Trip | tuple[Constraint, ...] | None:
    """Judge answer texts, then the answer's opening under the confirmation context.

    ``answer`` is the answer's own opening, never a window into it: the
    opening form is anchored at its start.  In prefix mode the opening is
    judged only once at least MAX_MATCH_CHARS of it exist ("Yes" may still
    become "Yesterday"); the lag holds more than that, so nothing of an answer
    is released before its opening is decided.
    """

    constraints: list[Constraint] = []
    for text in texts:
        result = judge_text(text, supplied, contexts, prefix=prefix, since=since)
        if isinstance(result, Trip):
            return result
        if result:
            constraints.extend(result)
    # The confirmation context is anchored at the answer's opening, never at
    # another output item.
    opening_decided = not prefix or len(answer) >= MAX_MATCH_CHARS
    if opening_decided and AFFIRMATION_OPEN.search(answer[:MAX_MATCH_CHARS]):
        # The bare opening names no target: the first family in FAMILIES order
        # whose context matched takes it.
        for family in FAMILIES:
            if family.name in contexts and family.confirmation is not None:
                return Trip(family.name, family.confirmation[1])
    return tuple(constraints) if prefix and constraints else None


def judge_message(
    message: Mapping[str, object], messages: list[object], assistant_position: int
) -> Trip | None:
    """Read the body's context and judge every rendered assistant text (the outlet's judgement)."""

    supplied, contexts = message_context(messages, assistant_position)
    answer = message.get("content")
    result = judge_rendered(
        rendered_texts(message),
        answer if isinstance(answer, str) else "",
        supplied,
        contexts,
    )
    return result if isinstance(result, Trip) else None


def judge_floor(since: int) -> int:
    """The start of the window a stream judges: every hit still decidable starts after ``since`` less
    the match bound, and its exemption context reaches back EXEMPTION_REACH_CHARS at most."""

    return max(0, since - MAX_MATCH_CHARS - EXEMPTION_REACH_CHARS)
