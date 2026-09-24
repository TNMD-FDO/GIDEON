"""Load turn cases and compile their checks.

A seed positive may carry ``must_not``, the patterns naming the figure its
prompt resolves; the row shows the check beside the turn class, while the
positive's expectation stays ``refused``. A seed control cannot carry
``must_not``, and no seed case carries ``must``, so a seed never holds a check
the eval set's copy of it cannot.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml  # type: ignore[import-untyped]

from gideon.host.report import Problem

_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.-]+")
EXPECTATIONS: Final[frozenset[str]] = frozenset(
    {"refused", "answered", "not-confirmed", "recorded"}
)
PRESENCE_VALUES: Final[frozenset[str]] = frozenset({"present", "absent", "any"})
# A case's kind: the seed's two, and the cases file's one; the summary counts by it.
CASE_KINDS: Final[tuple[str, ...]] = ("positive", "control", "case")
_SEED_KINDS: Final[frozenset[str]] = frozenset(CASE_KINDS) - {"case"}
_CASES_FIX: Final[str] = "Correct the cases file, then retry."


@dataclass(frozen=True, slots=True)
class Case:
    """One case with its expected class and optional content checks."""

    id: str
    prompt: str
    expect: str
    must: tuple[re.Pattern[str], ...] = ()
    must_not: tuple[re.Pattern[str], ...] = ()
    block: str = "any"
    kind: str = "case"
    search: bool = False
    sources: str = "any"


@dataclass(frozen=True, slots=True)
class CaseSet:
    """The cases loaded for one run and the human-readable source origin."""

    cases: tuple[Case, ...]
    origin: str
    searched: int = 0


def _problem(path: Path, detail: str) -> Problem:
    return Problem(f"cases file {path}: {detail}", _CASES_FIX)


def _compile_checks(
    case_path: Path, identifier: str, value: object, field: str
) -> tuple[re.Pattern[str], ...] | Problem:
    if isinstance(value, str):
        patterns = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        patterns = value
    else:
        return _problem(
            case_path,
            f"case {identifier!r} has an invalid {field}; expected a regex string or list.",
        )
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            return _problem(
                case_path,
                f"case {identifier!r} has an invalid {field} regex {pattern!r}: {exc}.",
            )
    return tuple(compiled)


def _case_entry(
    case_path: Path,
    value: object,
    index: int,
    *,
    seed: bool,
) -> Case | Problem:
    if not isinstance(value, dict):
        return _problem(case_path, f"case {index} must be a mapping.")
    identifier_value = value.get("id")
    if (
        not isinstance(identifier_value, str)
        or not identifier_value
        or _ID.fullmatch(identifier_value) is None
    ):
        return _problem(case_path, f"case {index} has an invalid id.")
    identifier = identifier_value
    if seed and "search" in value:
        return _problem(case_path, f"seed case {identifier!r} cannot carry search.")
    if seed and "sources" in value:
        return _problem(case_path, f"seed case {identifier!r} cannot carry sources.")
    prompt_value = value.get("prompt")
    if not isinstance(prompt_value, str) or not prompt_value:
        return _problem(case_path, f"case {identifier!r} has an invalid prompt.")
    prompt = prompt_value
    if "supersedes" in value and not isinstance(value["supersedes"], str):
        label = "seed case" if seed else "case"
        return _problem(
            case_path,
            f"{label} {identifier!r} has an invalid supersedes target.",
        )

    if seed:
        kind = value.get("kind")
        if not isinstance(kind, str) or kind not in _SEED_KINDS:
            return _problem(
                case_path,
                f"seed case {identifier!r} has an unknown kind {kind!r}.",
            )
        if "must" in value:
            return _problem(case_path, f"seed case {identifier!r} cannot carry must.")
        seed_must_not: tuple[re.Pattern[str], ...] = ()
        if "must_not" in value:
            if kind != "positive":
                return _problem(
                    case_path,
                    f"seed control {identifier!r} cannot carry must_not.",
                )
            loaded = _compile_checks(case_path, identifier, value["must_not"], "must_not")
            if isinstance(loaded, Problem):
                return loaded
            seed_must_not = loaded
        expect = "refused" if kind == "positive" else "recorded"
        return Case(identifier, prompt, expect, must_not=seed_must_not, kind=kind)

    expected_value = value.get("expect")
    if not isinstance(expected_value, str) or expected_value not in EXPECTATIONS:
        return _problem(case_path, f"case {identifier!r} has an invalid expectation.")
    block = value.get("block", "any")
    if not isinstance(block, str) or block not in PRESENCE_VALUES:
        return _problem(case_path, f"case {identifier!r} has an invalid block.")
    sources = value.get("sources", "any")
    if not isinstance(sources, str) or sources not in PRESENCE_VALUES:
        return _problem(case_path, f"case {identifier!r} has an invalid sources.")
    must: tuple[re.Pattern[str], ...] = ()
    must_not: tuple[re.Pattern[str], ...] = ()
    if "must" in value:
        loaded = _compile_checks(case_path, identifier, value["must"], "must")
        if isinstance(loaded, Problem):
            return loaded
        must = loaded
    if "must_not" in value:
        loaded = _compile_checks(case_path, identifier, value["must_not"], "must_not")
        if isinstance(loaded, Problem):
            return loaded
        must_not = loaded
    search = value.get("search", False)
    if not isinstance(search, bool):
        return _problem(
            case_path,
            f"case {identifier!r} has an invalid search; expected true or false.",
        )
    return Case(
        identifier, prompt, expected_value, must, must_not, block, search=search, sources=sources
    )


def _retained_cases(
    case_path: Path, values: list[object], *, seed: bool
) -> tuple[list[Case], int] | Problem:
    """Load every entry, then drop each case another names by ``supersedes:``."""

    loaded: list[Case] = []
    identifiers: set[str] = set()
    superseded: set[str] = set()
    for index, value in enumerate(values, start=1):
        case = _case_entry(case_path, value, index, seed=seed)
        if isinstance(case, Problem):
            return case
        if case.id in identifiers:
            return _problem(case_path, f"case id {case.id!r} is repeated.")
        identifiers.add(case.id)
        loaded.append(case)
        if isinstance(value, dict) and isinstance(value.get("supersedes"), str):
            superseded.add(value["supersedes"])
    for target in superseded:
        if target not in identifiers:
            return _problem(case_path, f"case supersedes unknown case {target!r}.")
    return [case for case in loaded if case.id not in superseded], len(superseded)


def _load_seed(case_path: Path, document: dict[object, object]) -> CaseSet | Problem:
    family = document.get("family")
    version = document.get("pattern_set_version")
    if (
        not isinstance(family, str)
        or not family
        or isinstance(version, bool)
        or not isinstance(version, (str, int))
    ):
        return _problem(case_path, "seed requires family and pattern_set_version.")
    values = document.get("cases")
    if not isinstance(values, list) or not values:
        return _problem(case_path, "seed must contain a non-empty cases list.")
    retained = _retained_cases(case_path, values, seed=True)
    if isinstance(retained, Problem):
        return retained
    loaded, _ = retained
    counts = {
        "positive": sum(case.kind == "positive" for case in loaded),
        "control": sum(case.kind == "control" for case in loaded),
    }
    origin = (
        f"seed {family} set {version}: {counts['positive']} positives, "
        f"{counts['control']} controls"
    )
    return CaseSet(tuple(loaded), origin)


def _load_cases_file(case_path: Path, values: object) -> CaseSet | Problem:
    if not isinstance(values, list):
        return _problem(case_path, "must contain a cases list.")
    if not values:
        return _problem(case_path, "must contain at least one case.")
    retained = _retained_cases(case_path, values, seed=False)
    if isinstance(retained, Problem):
        return retained
    loaded, superseded = retained
    suffix = f" ({superseded} superseded)" if superseded else ""
    return CaseSet(
        tuple(loaded),
        f"cases file {case_path}: {len(loaded)} cases{suffix}",
        sum(case.search for case in loaded),
    )


def load_cases(path: str | Path) -> CaseSet | Problem:
    """Read a seed or cases document and return its validated, compiled set."""

    case_path = Path(path)
    try:
        document = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _problem(case_path, "is missing.")
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return _problem(case_path, f"could not be read: {exc}.")

    if not isinstance(document, dict):
        return _problem(case_path, "must contain a mapping at its top level.")
    seed = "family" in document or "pattern_set_version" in document
    if seed:
        return _load_seed(case_path, document)
    return _load_cases_file(case_path, document.get("cases"))


def select_cases(case_set: CaseSet, requested_ids: Sequence[str]) -> CaseSet | Problem:
    """Return the retained cases named by the operator, in file order."""

    requested: list[str] = []
    seen: set[str] = set()
    for identifier in requested_ids:
        if identifier not in seen:
            requested.append(identifier)
            seen.add(identifier)
    available = {case.id for case in case_set.cases}
    absent = [identifier for identifier in requested if identifier not in available]
    if absent:
        absent_text = ", ".join(absent)
        return Problem(
            f"requested case ids are absent: {absent_text}.",
            "Name ids the cases file holds; a superseded case is retired, then retry.",
        )
    selected = tuple(case for case in case_set.cases if case.id in seen)
    return CaseSet(
        selected,
        f"{case_set.origin}; {len(selected)} of {len(case_set.cases)} selected",
        sum(case.search for case in selected),
    )
