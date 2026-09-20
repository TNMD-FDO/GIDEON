"""Standard-library axis definitions for extraction-question variants."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

Label = Mapping[str, object]
AxisFunction = Callable[[str, Label], "Edit | None"]

RESPELLED_TYPES = frozenset(
    {
        "statute",
        "guideline",
        "court_rule",
        "bare_section",
        "bare_rule",
        "regulation",
        "appendix_statute",
        "habeas_rule",
        "scotus_rule",
        "docket",
    }
)


@dataclass(frozen=True, slots=True)
class Edit:
    """A replacement range and the object range inside its replacement."""

    start: int
    end: int
    replacement: str
    object_start: int
    object_end: int


@dataclass(frozen=True, slots=True)
class Axis:
    """One versioned, question-only respelling operation."""

    name: str
    version: int
    function: AxisFunction

    @property
    def axis_id(self) -> str:
        return f"{self.name}@{self.version}"


_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_DESIGNATORS = re.compile(r"(?:\([^()]*\))+")
_GUIDELINE_ID = re.compile(r"\d[A-Za-z]\d{1,3}\.\d{1,3}")
_SECTION_TOKEN = re.compile(r"\d+(?:[.]\d+)?(?:[A-Za-z])?(?:-[A-Za-z0-9]+)?")
_CODE_TOKEN = re.compile(
    r"U\.S\.C\.|USC|U\.S\. Code|United States Code|C\.F\.R\.|CFR", re.IGNORECASE
)
_MARKER_BEFORE = re.compile(r"(?:§{1,2}|sections?|secs?\.)[  ]*$", re.IGNORECASE)
_ANNOTATED_CODE = re.compile(r"U\.?S\.?C\.?A\.?(?![A-Za-z])", re.IGNORECASE)
"""The annotated edition: an axis respells no code token inside it."""
_RULE_WORD = re.compile(r"\bRule\b|\bR\.", re.IGNORECASE)
_APPENDIX_ORDINAL = re.compile(
    r"\b(?:app\.?|appendix)[  ]+(?:\d+|[IVXLCDM]+)", re.IGNORECASE
)
_SINGULAR_SIGN = re.compile(r"(?<!§)§[  ]?(?!§)")
_SPELLED_MARKER = re.compile(r"(?:section|sec\.)[  ]", re.IGNORECASE)


def _label_type(label: Label) -> str:
    value = label.get("type")
    return value if isinstance(value, str) else ""


def _label_text(question: str, label: Label) -> str | None:
    start = label.get("start")
    end = label.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
        return None
    if end > len(question):
        return None
    return question[start:end]


def _key_token(label: Label) -> str | None:
    """The section, id, or rule number the label's key states, or ``None``."""

    key = label.get("key")
    if not isinstance(key, str):
        return None
    object_type = _label_type(label)
    if object_type in {"statute", "appendix_statute"}:
        return key.rsplit("/s", 1)[-1]
    if object_type in {"regulation", "guideline"}:
        return key.rsplit("/", 1)[-1]
    if object_type in {"court_rule", "habeas_rule", "scotus_rule"}:
        return key.rsplit("rule", 1)[-1]
    return None


def _token_span(text: str, token: str, paragraph: bool) -> tuple[int, int] | None:
    tail = r"(?:\.\d+)?" if paragraph else ""
    pattern = rf"(?<![0-9A-Za-z.]){re.escape(token)}{tail}(?![0-9A-Za-z])"
    matches = list(re.finditer(pattern, text, re.IGNORECASE))
    return matches[-1].span() if matches else None


def _number_span(text: str, label: Label) -> tuple[int, int] | None:
    """The object's own number: the key's token, else the text's first one."""

    object_type = _label_type(label)
    if object_type == "docket":
        return 0, len(text)
    token = _key_token(label)
    if token is not None:
        span = _token_span(text, token, object_type == "scotus_rule")
        if span is not None:
            return span
    if object_type == "bare_rule":
        rule = list(_RULE_WORD.finditer(text))
        if rule:
            after_rule = list(_NUMBER.finditer(text, rule[-1].end()))
            if after_rule:
                return after_rule[0].span()
    match = _SECTION_TOKEN.search(text)
    return match.span() if match is not None else None


def _core_span(text: str, label: Label) -> tuple[int, int] | None:
    number = _number_span(text, label)
    if number is None:
        return None
    end = number[1]
    designators = _DESIGNATORS.match(text, end)
    if designators is not None:
        end = designators.end()
    return number[0], end


def kept_core(text: str, label: Label) -> str:
    """Return the section/rule core whose bytes every axis must preserve."""

    if _label_type(label) == "docket":
        return text
    span = _core_span(text, label)
    return text[span[0] : span[1]] if span is not None else ""


def _protected_spans(text: str, label: Label) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    core = _core_span(text, label)
    if core is not None:
        spans.append(core)
    if _label_type(label) == "appendix_statute":
        ordinal = _APPENDIX_ORDINAL.search(text)
        if ordinal is not None:
            number = re.search(r"(?:\d+|[IVXLCDM]+)$", ordinal.group())
            if number is not None:
                spans.append((ordinal.start() + number.start(), ordinal.end()))
    return tuple(spans)


def _whole_edit(text: str, replacement: str) -> Edit | None:
    if replacement == text:
        return None
    return Edit(0, len(text), replacement, 0, len(replacement))


def _edit_label(question: str, label: Label, replacement: str) -> Edit | None:
    start = label.get("start")
    end = label.get("end")
    text = _label_text(question, label)
    if not isinstance(start, int) or not isinstance(end, int) or text is None:
        return None
    edit = _whole_edit(text, replacement)
    if edit is None:
        return None
    return Edit(start, end, edit.replacement, edit.object_start, edit.object_end)


def _code_target(object_type: str, axis: str) -> str | None:
    if object_type in {"statute", "appendix_statute"}:
        return {"dotted": "U.S.C.", "bare": "USC", "word": "U.S. Code"}[axis]
    if object_type == "regulation":
        return {"dotted": "C.F.R.", "bare": "CFR"}.get(axis)
    return None


def _code_edit(question: str, label: Label, axis: str) -> Edit | None:
    object_type = _label_type(label)
    target = _code_target(object_type, axis)
    text = _label_text(question, label)
    if target is None or text is None:
        return None
    if _ANNOTATED_CODE.search(text):
        return None
    match = _CODE_TOKEN.search(text)
    if match is None:
        return None
    replacement = text[: match.start()] + target + text[match.end() :]
    return _edit_label(question, label, replacement)


def code_dotted(question: str, label: Label) -> Edit | None:
    return _code_edit(question, label, "dotted")


def code_bare(question: str, label: Label) -> Edit | None:
    return _code_edit(question, label, "bare")


def code_word(question: str, label: Label) -> Edit | None:
    return _code_edit(question, label, "word")


def sign_present(question: str, label: Label) -> Edit | None:
    object_type = _label_type(label)
    text = _label_text(question, label)
    if text is None or object_type not in {"statute", "regulation", "guideline", "bare_section"}:
        return None
    if object_type in {"statute", "regulation"} and _CODE_TOKEN.search(text) is None:
        return None
    core = _core_span(text, label)
    if core is None:
        return None
    insertion = core[0]
    if _MARKER_BEFORE.search(text[:insertion]):
        return None
    replacement = text[:insertion] + "§ " + text[insertion:]
    start = label.get("start")
    end = label.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return Edit(start, end, replacement, 0, len(replacement))


def sign_absent(question: str, label: Label) -> Edit | None:
    object_type = _label_type(label)
    text = _label_text(question, label)
    if text is None or object_type not in {"statute", "regulation", "guideline", "bare_section"}:
        return None
    if object_type in {"statute", "regulation"} and _CODE_TOKEN.search(text) is None:
        return None
    if object_type == "bare_section":
        core = _core_span(text, label)
        if (
            core is None
            or "." in text[core[0] : core[1]]
            or "(" not in text[core[0] : core[1]]
        ):
            return None
    match = _SINGULAR_SIGN.search(text)
    if match is None:
        match = _SPELLED_MARKER.search(text)
    if match is None:
        return None
    replacement = text[: match.start()] + text[match.end() :]
    return _edit_label(question, label, replacement)


def spacing_tight(question: str, label: Label) -> Edit | None:
    text = _label_text(question, label)
    if text is None:
        return None
    replacement = re.sub(r"(§{1,2})[ \u00a0]", r"\1", text, count=1)
    return _edit_label(question, label, replacement)


def spacing_nbsp(question: str, label: Label) -> Edit | None:
    text = _label_text(question, label)
    if text is None:
        return None
    return _edit_label(question, label, text.replace(" ", "\u00a0"))


_FEDERAL_FORMS: dict[str, tuple[str, str]] = {
    "Crim": ("Fed. R. Crim. P.", "Federal Rule of Criminal Procedure"),
    "Civ": ("Fed. R. Civ. P.", "Federal Rule of Civil Procedure"),
    "App": ("Fed. R. App. P.", "Federal Rule of Appellate Procedure"),
    "Evid": ("Fed. R. Evid.", "Federal Rule of Evidence"),
}
"""Each pilot set's short and long form, the two ends of the form axis."""
_HABEAS_SHORT = re.compile(r"\s*(?:§|Section)\s*225[45]\s+Rule\b", re.IGNORECASE)
_HABEAS_LONG = re.compile(r"Rules Governing", re.IGNORECASE)
_SCOTUS_SHORT = re.compile(r"\s*Sup\.\s*Ct\.\s*R\.", re.IGNORECASE)
_SCOTUS_LONG = re.compile(r"Supreme Court", re.IGNORECASE)
_FEDERAL_LONG = re.compile(r"Federal Rules? of", re.IGNORECASE)


def _rule_set(key: object) -> str | None:
    if not isinstance(key, str):
        return None
    if "/Crim/" in key:
        return "Crim"
    if "/Civil/" in key:
        return "Civ"
    if "/App/" in key:
        return "App"
    if "/Evid/" in key:
        return "Evid"
    if key.startswith("rules/2254/"):
        return "2254"
    if key.startswith("rules/2255/"):
        return "2255"
    if key.startswith("rules/scotus/"):
        return "scotus"
    return None


def form_long_short(question: str, label: Label) -> Edit | None:
    object_type = _label_type(label)
    text = _label_text(question, label)
    if text is None or object_type not in {"court_rule", "habeas_rule", "scotus_rule"}:
        return None
    rule_set = _rule_set(label.get("key"))
    core = _core_span(text, label)
    if rule_set is None or core is None:
        return None
    number = text[core[0] : core[1]]
    if object_type == "habeas_rule":
        set_words = "Cases" if rule_set == "2254" else "Proceedings"
        if _HABEAS_SHORT.match(text):
            replacement = (
                f"Rule {number} of the Rules Governing Section {rule_set} {set_words}"
            )
        elif _HABEAS_LONG.search(text):
            replacement = f"§ {rule_set} Rule {number}"
        else:
            return None
    elif object_type == "scotus_rule":
        if _SCOTUS_SHORT.match(text):
            replacement = f"Supreme Court Rule {number}"
        elif _SCOTUS_LONG.search(text):
            replacement = f"Sup. Ct. R. {number}"
        else:
            return None
    else:
        short_form, long_form = _FEDERAL_FORMS[rule_set]
        form = short_form if _FEDERAL_LONG.search(text) else long_form
        replacement = f"{form} {number}"
    return _edit_label(question, label, replacement)


_DOCKET_MARKERS = ("No.", "Case No.", "Docket No.", "Dkt.")
_DISTRICT = re.compile(r"\d:\d{2}-[A-Za-z]{2}-\d{3,5}(?:-[A-Za-z]{2,4}){0,3}(?:-\d{1,3})?$")
_TWO_PART = re.compile(r"\d{2}-\d{1,5}$")


def _left_marker(question: str, start: int) -> tuple[int, str] | None:
    prefix = question[:start]
    for marker in sorted(_DOCKET_MARKERS, key=len, reverse=True):
        match = re.search(re.escape(marker) + r"[ \u00a0]+$", prefix)
        if match is not None:
            return match.start(), match.group().strip()
    return None


def docket_marker(question: str, label: Label) -> Edit | None:
    if _label_type(label) != "docket":
        return None
    text = _label_text(question, label)
    start = label.get("start")
    end = label.get("end")
    if text is None or not isinstance(start, int) or not isinstance(end, int):
        return None
    marker = _left_marker(question, start)
    two_part = _TWO_PART.fullmatch(text) is not None
    if two_part and marker is None:
        return None
    if marker is None:
        if _DISTRICT.fullmatch(text) is None:
            return None
        replacement = "No. " + text
        return Edit(start, end, replacement, 4, len(replacement))
    marker_start, current = marker
    if two_part:
        current_index = _DOCKET_MARKERS.index(current)
        next_marker = _DOCKET_MARKERS[(current_index + 1) % len(_DOCKET_MARKERS)]
        replacement = next_marker + " " + text
        return Edit(marker_start, end, replacement, len(next_marker) + 1, len(replacement))
    if _DISTRICT.fullmatch(text) is None:
        return None
    return Edit(marker_start, end, text, 0, len(text))


def lower_case(question: str, label: Label) -> Edit | None:
    object_type = _label_type(label)
    text = _label_text(question, label)
    if text is None or object_type not in RESPELLED_TYPES or object_type == "docket":
        return None
    protected = _protected_spans(text, label)
    chars = list(text.lower())
    for start, end in protected:
        chars[start:end] = text[start:end]
    return _edit_label(question, label, "".join(chars))


AXES = (
    Axis("code-dotted", 1, code_dotted),
    Axis("code-bare", 1, code_bare),
    Axis("code-word", 1, code_word),
    Axis("sign-present", 1, sign_present),
    Axis("sign-absent", 1, sign_absent),
    Axis("spacing-tight", 1, spacing_tight),
    Axis("spacing-nbsp", 1, spacing_nbsp),
    Axis("form-long-short", 1, form_long_short),
    Axis("docket-marker", 1, docket_marker),
    Axis("lower-case", 1, lower_case),
)
CURRENT = {axis.name: axis for axis in AXES}
BY_ID = {axis.axis_id: axis for axis in AXES}
