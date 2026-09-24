"""Load and validate the committed trigger-registry file format.

The root mapping has ``version`` and a non-empty ``triggers`` list. Each entry
has ``id``, ``reopens``, ``register``, ``measure``, ``state``, ``condition``,
``says``, ``ruled``, and ``baseline``. ``register``, ``condition``, ``ruled``,
and ``baseline`` are optional in the shape; ``condition`` is required for a
``watching`` entry and refused for ``acted`` or ``retired`` entries, while
``ruled`` is refused for ``watching`` and required for the other states.

A condition is one clause or an ``all`` mapping of non-empty clause lists. A
clause has ``figure``, ``op``, and ``value``, with optional ``runs``. The
allowed operations are ``above``, ``at-least``, and ``below``. The measure
vocabulary is ``unavailable`` and ``eval-set``; the latter supplies
``judged_queries`` and ``held_out_ids``. A clause under ``unavailable`` may
name any well-formed figure because that measure has no figures yet. Every
finding carries the one fix: edit the file and consult the improvement leaf.
"""

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal, cast

import yaml  # type: ignore[import-untyped]

from gideon.host.sysio import Host, PathLike, RealHost

type TriggerState = Literal["watching", "acted", "retired"]
type TriggerOp = Literal["above", "at-least", "below"]
type TriggerMeasure = Literal["unavailable", "eval-set"]


@dataclass(frozen=True, slots=True)
class Clause:
    """One threshold clause in a trigger condition."""

    figure: str
    op: TriggerOp
    value: int | float
    runs: int = 1


@dataclass(frozen=True, slots=True)
class Condition:
    """The clauses that must hold for a trigger."""

    clauses: tuple[Clause, ...]


@dataclass(frozen=True, slots=True)
class Trigger:
    """One committed improvement trigger."""

    id: str
    reopens: str
    register: str | None
    measure: TriggerMeasure
    state: TriggerState
    condition: Condition | None
    says: str
    ruled: str | None
    baseline: int | float | None


@dataclass(frozen=True, slots=True)
class TriggerRegistry:
    """The validated trigger registry and its report-oriented indexes."""

    version: int
    triggers: tuple[Trigger, ...]
    by_id: Mapping[str, Trigger]
    watching: tuple[Trigger, ...]


@dataclass(frozen=True, slots=True)
class TriggerError:
    """A structured refusal from trigger-registry loading or validation."""

    key_path: str | None
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class TriggerRegistryLoadResult:
    """The parsed registry, or every error found while loading it."""

    registry: TriggerRegistry | None = None
    document: Mapping[str, object] | None = None
    errors: tuple[TriggerError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.registry is not None


class _DuplicateKeyError(yaml.YAMLError):
    pass


class TriggerLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys."""


TriggerLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: TriggerLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


TriggerLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


_FIX: Final = "Edit config/triggers.yaml; consult docs/runbooks/release-files.md §7."
_ROOT_KEYS: Final = ("version", "triggers")
_ENTRY_KEYS: Final = (
    "id",
    "reopens",
    "register",
    "measure",
    "state",
    "condition",
    "says",
    "ruled",
    "baseline",
)
_CLAUSE_KEYS: Final = ("figure", "op", "value", "runs")
_CONDITION_KEYS: Final = ("all",)
STATES: Final[tuple[TriggerState, ...]] = ("watching", "acted", "retired")
OPS: Final[tuple[TriggerOp, ...]] = ("above", "at-least", "below")
MEASURES: Final[Mapping[TriggerMeasure, tuple[str, ...]]] = {
    "unavailable": (),
    "eval-set": ("judged_queries", "held_out_ids"),
}
ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FIGURE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")
SECTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^§[0-9]+(?:\.[0-9]+)?$")
REGISTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[EM][0-9]{2}$")
RULED_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} \S+$")
SCHEMA_VERSION: Final[int] = 1


def _path(prefix: str, key: object) -> str:
    rendered = str(key)
    return f"{prefix}.{rendered}" if prefix else rendered


def _nearest(path: str, valid: Sequence[str]) -> str:
    return difflib.get_close_matches(path, list(valid), n=1, cutoff=0.0)[0]


def _unknown(path: str, valid: Sequence[str]) -> TriggerError:
    return TriggerError(
        key_path=path,
        problem=(
            f"Unknown key '{path}'; nearest valid key is '{_nearest(path, valid)}'."
        ),
        fix=_FIX,
    )


def _walk_unknown(
    value: object,
    keys: Sequence[str],
    prefix: str,
    errors: list[TriggerError],
) -> None:
    if not isinstance(value, Mapping):
        return
    for key in value:
        path = _path(prefix, key)
        if not isinstance(key, str) or key not in keys:
            errors.append(_unknown(path, keys))


def _lookup(document: Mapping[str, object], path: str) -> tuple[bool, object]:
    current: object = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _error(path: str, detail: str) -> TriggerError:
    return TriggerError(
        key_path=path,
        problem=f"Invalid value for '{path}': {detail}.",
        fix=_FIX,
    )


def _required_string(
    value: Mapping[str, object],
    key: str,
    path: str,
    errors: list[TriggerError],
) -> str | None:
    if key not in value:
        errors.append(_error(path, "missing required key"))
        return None
    item = value[key]
    if not isinstance(item, str) or not item:
        errors.append(_error(path, "expected a non-empty string"))
        return None
    return item


def _number(
    value: Mapping[str, object],
    key: str,
    path: str,
    errors: list[TriggerError],
) -> int | float | None:
    if key not in value:
        errors.append(_error(path, "missing required key"))
        return None
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        errors.append(_error(path, "expected an integer or float"))
        return None
    return item


def _validate_clause(
    value: object,
    path: str,
    measure: str | None,
    errors: list[TriggerError],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(_error(path, "expected a mapping"))
        return
    _walk_unknown(value, _CLAUSE_KEYS, path, errors)
    item = cast(Mapping[str, object], value)
    figure = _required_string(item, "figure", f"{path}.figure", errors)
    if figure is not None:
        if FIGURE_PATTERN.fullmatch(figure) is None:
            errors.append(
                _error(
                    f"{path}.figure",
                    "expected a lowercase letter followed by lowercase letters, digits, or underscores",
                )
            )
        elif measure in MEASURES and measure != "unavailable":
            supplied = MEASURES[cast(TriggerMeasure, measure)]
            if figure not in supplied:
                errors.append(
                    _error(
                        f"{path}.figure",
                        f"measure '{measure}' does not supply this figure",
                    )
                )

    if "op" not in item:
        errors.append(_error(f"{path}.op", "missing required key"))
    else:
        op = item["op"]
        if not isinstance(op, str):
            errors.append(_error(f"{path}.op", "expected a string"))
        elif op not in OPS:
            errors.append(
                _error(
                    f"{path}.op",
                    f"unknown operation '{op}'; nearest valid operation is '{_nearest(op, OPS)}'",
                )
            )

    _number(item, "value", f"{path}.value", errors)
    if "runs" in item:
        runs = item["runs"]
        if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
            errors.append(_error(f"{path}.runs", "expected an integer of at least one"))


def _validate_condition(
    value: object,
    path: str,
    measure: str | None,
    errors: list[TriggerError],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(_error(path, "expected a mapping"))
        return
    item = cast(Mapping[str, object], value)
    if "all" in item:
        _walk_unknown(item, _CONDITION_KEYS, path, errors)
        all_value = item["all"]
        if not isinstance(all_value, list) or not all_value:
            errors.append(_error(f"{path}.all", "expected a non-empty list of clauses"))
            return
        for index, clause in enumerate(all_value):
            _validate_clause(clause, f"{path}.all[{index}]", measure, errors)
        return
    _validate_clause(item, path, measure, errors)


def _validate_ruled(value: object, path: str, errors: list[TriggerError]) -> None:
    if not isinstance(value, str) or RULED_PATTERN.fullmatch(value) is None:
        errors.append(
            _error(
                path,
                "expected an ISO date followed by a space and a tag or pull request",
            )
        )
        return
    try:
        date.fromisoformat(value.split(" ", 1)[0])
    except ValueError:
        errors.append(
            _error(path, "expected a valid ISO date followed by a tag or pull request")
        )


def validate_trigger_registry(document: Mapping[str, object]) -> list[TriggerError]:
    """Return every shape, grammar, vocabulary, and state-rule finding."""

    errors: list[TriggerError] = []
    _walk_unknown(document, _ROOT_KEYS, "", errors)

    present, version = _lookup(document, "version")
    if not present:
        errors.append(_error("version", "missing required key"))
    elif not isinstance(version, int) or isinstance(version, bool):
        errors.append(_error("version", "expected schema version 1"))
    elif version != SCHEMA_VERSION:
        errors.append(_error("version", f"expected schema version {SCHEMA_VERSION}"))

    present, triggers_value = _lookup(document, "triggers")
    if not present:
        errors.append(_error("triggers", "missing required key"))
        return errors
    if not isinstance(triggers_value, list) or not triggers_value:
        errors.append(_error("triggers", "expected a non-empty list"))
        return errors

    seen_ids: set[str] = set()
    for index, entry in enumerate(triggers_value):
        entry_path = f"triggers[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(_error(entry_path, "expected a mapping"))
            continue
        item = cast(Mapping[str, object], entry)
        _walk_unknown(item, _ENTRY_KEYS, entry_path, errors)

        identifier = _required_string(item, "id", f"{entry_path}.id", errors)
        if identifier is not None:
            if ID_PATTERN.fullmatch(identifier) is None:
                errors.append(
                    _error(
                        f"{entry_path}.id",
                        "expected lowercase letters, digits, and hyphens",
                    )
                )
            if identifier in seen_ids:
                errors.append(
                    _error(f"{entry_path}.id", "must be unique; this is a duplicate id")
                )
            seen_ids.add(identifier)

        reopens = _required_string(item, "reopens", f"{entry_path}.reopens", errors)
        if reopens is not None and SECTION_PATTERN.fullmatch(reopens) is None:
            errors.append(
                _error(
                    f"{entry_path}.reopens",
                    "expected a section reference: the section sign, then a number such as 18.4",
                )
            )

        if "register" in item:
            register = item["register"]
            if (
                not isinstance(register, str)
                or REGISTER_PATTERN.fullmatch(register) is None
            ):
                errors.append(
                    _error(
                        f"{entry_path}.register",
                        "expected a register figure such as E20 or M18",
                    )
                )

        measure = _required_string(item, "measure", f"{entry_path}.measure", errors)
        if measure is not None and measure not in MEASURES:
            errors.append(
                _error(
                    f"{entry_path}.measure",
                    f"unknown measure '{measure}'; nearest valid measure is '{_nearest(measure, tuple(MEASURES))}'",
                )
            )

        state = _required_string(item, "state", f"{entry_path}.state", errors)
        if state is not None and state not in STATES:
            errors.append(
                _error(
                    f"{entry_path}.state",
                    f"unknown state '{state}'; nearest valid state is '{_nearest(state, STATES)}'",
                )
            )

        _required_string(item, "says", f"{entry_path}.says", errors)

        if "baseline" in item:
            baseline = item["baseline"]
            if isinstance(baseline, bool) or not isinstance(baseline, (int, float)):
                errors.append(
                    _error(f"{entry_path}.baseline", "expected an integer or float")
                )

        if "condition" in item:
            if state in ("acted", "retired"):
                errors.append(
                    _error(
                        f"{entry_path}.condition",
                        f"must be absent when state is '{state}'",
                    )
                )
            _validate_condition(
                item["condition"], f"{entry_path}.condition", measure, errors
            )
        elif state == "watching":
            errors.append(
                _error(
                    f"{entry_path}.condition",
                    "missing required key for a watching entry",
                )
            )

        if "ruled" in item:
            if state == "watching":
                errors.append(
                    _error(
                        f"{entry_path}.ruled", "must be absent when state is 'watching'"
                    )
                )
            _validate_ruled(item["ruled"], f"{entry_path}.ruled", errors)
        elif state in ("acted", "retired"):
            errors.append(
                _error(
                    f"{entry_path}.ruled", f"missing required key for a {state} entry"
                )
            )

    return errors


def _construct(document: Mapping[str, object]) -> TriggerRegistry:
    raw_triggers = cast(list[Mapping[str, object]], document["triggers"])
    triggers: list[Trigger] = []
    for entry in raw_triggers:
        condition_value = entry.get("condition")
        condition: Condition | None = None
        if isinstance(condition_value, Mapping):
            if "all" in condition_value:
                raw_clauses = cast(list[Mapping[str, object]], condition_value["all"])
            else:
                raw_clauses = [cast(Mapping[str, object], condition_value)]
            condition = Condition(
                clauses=tuple(
                    Clause(
                        figure=cast(str, clause["figure"]),
                        op=cast(TriggerOp, clause["op"]),
                        value=cast(int | float, clause["value"]),
                        runs=cast(int, clause.get("runs", 1)),
                    )
                    for clause in raw_clauses
                )
            )
        measure = cast(TriggerMeasure, entry["measure"])
        state = cast(TriggerState, entry["state"])
        triggers.append(
            Trigger(
                id=cast(str, entry["id"]),
                reopens=cast(str, entry["reopens"]),
                register=cast(str | None, entry.get("register")),
                measure=measure,
                state=state,
                condition=condition,
                says=cast(str, entry["says"]),
                ruled=cast(str | None, entry.get("ruled")),
                baseline=cast(int | float | None, entry.get("baseline")),
            )
        )
    trigger_tuple = tuple(triggers)
    return TriggerRegistry(
        version=cast(int, document["version"]),
        triggers=trigger_tuple,
        by_id={trigger.id: trigger for trigger in trigger_tuple},
        watching=tuple(
            trigger for trigger in trigger_tuple if trigger.state == "watching"
        ),
    )


def render_errors(errors: Sequence[TriggerError]) -> str:
    """Render one refusal per line, with its corrective action last."""

    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )


def read_trigger_registry(
    path: PathLike, *, host: Host | None = None
) -> TriggerRegistryLoadResult:
    """Read and parse a registry file without constructing its dataclasses."""

    io = host or RealHost()
    try:
        text = io.read_text(path)
    except FileNotFoundError:
        return TriggerRegistryLoadResult(
            errors=(TriggerError(None, f"trigger registry is missing: {path}", _FIX),)
        )
    except PermissionError:
        return TriggerRegistryLoadResult(
            errors=(
                TriggerError(
                    None,
                    f"trigger registry is unreadable due to permissions: {path}",
                    _FIX,
                ),
            )
        )
    except UnicodeDecodeError:
        return TriggerRegistryLoadResult(
            errors=(
                TriggerError(
                    None, f"trigger registry is not valid UTF-8: {path}", _FIX
                ),
            )
        )
    except OSError as exc:
        return TriggerRegistryLoadResult(
            errors=(
                TriggerError(
                    None, f"trigger registry is unreadable: {path} ({exc})", _FIX
                ),
            )
        )

    try:
        document = yaml.load(text, Loader=TriggerLoader)
    except _DuplicateKeyError as exc:
        return TriggerRegistryLoadResult(
            errors=(TriggerError(None, f"trigger registry has a {exc}", _FIX),)
        )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return TriggerRegistryLoadResult(
            errors=(
                TriggerError(
                    None,
                    f"trigger registry has a YAML parse error{location}: {exc}",
                    _FIX,
                ),
            )
        )
    if document is None:
        return TriggerRegistryLoadResult(
            errors=(TriggerError(None, "trigger registry is empty", _FIX),)
        )
    if not isinstance(document, Mapping):
        return TriggerRegistryLoadResult(
            errors=(
                TriggerError(None, "trigger registry root must be a mapping", _FIX),
            )
        )
    return TriggerRegistryLoadResult(document=cast(Mapping[str, object], document))


def load_trigger_registry(
    path: PathLike, *, host: Host | None = None
) -> TriggerRegistryLoadResult:
    """Parse, validate, and construct a trigger registry."""

    result = read_trigger_registry(path, host=host)
    if result.errors or result.document is None:
        return result
    errors = validate_trigger_registry(result.document)
    if errors:
        return TriggerRegistryLoadResult(document=result.document, errors=tuple(errors))
    return TriggerRegistryLoadResult(
        registry=_construct(result.document), document=result.document
    )


def default_triggers_path() -> Path:
    """Return the committed trigger registry path for this checkout."""

    return Path(__file__).parents[2] / "config/triggers.yaml"
