"""Strict loader for the hand-authored court geography table."""

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import yaml  # type: ignore[import-untyped]

from gideon.host.courts import CIRCUITS, STATE_CODES
from gideon.host.sysio import Host, PathLike, RealHost

LEVEL_BY_CODE: Final[Mapping[str, str]] = {
    "S": "state_supreme",
    "TS": "state_supreme",
    "SA": "state_appellate",
    "TA": "state_appellate",
}
"""The CSV jurisdiction codes that designate state-level courts."""

CSV_CODES: Final = (
    "F", "FD", "FB", "FBP", "FS",
    "S", "SA", "ST", "SS", "SAG",
    "TS", "TA", "TT",
    "TRS", "TRA", "TRT", "TRX",
    "MA", "C", "I", "T",
)
"""CourtListener's closed jurisdiction-code vocabulary: a correction names one.

A correction may move a row onto a state level or off it — a trial court or
an administrative body the CSV codes as appellate is corrected to its own code.
"""


@dataclass(frozen=True, slots=True)
class State:
    """One closed-list state or territory and its federal circuit."""

    name: str
    circuit: str | None


@dataclass(frozen=True, slots=True)
class Correction:
    """One explicit correction to an upstream jurisdiction code."""

    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class Geography:
    """The validated hand table used to join CSV rows to court geography."""

    states: Mapping[str, State]
    federal: Mapping[str, str | None]
    state_courts: Mapping[str, str]
    unplaced: Mapping[str, str]
    corrections: Mapping[str, Correction]


@dataclass(frozen=True, slots=True)
class GeographyError:
    """A structured refusal from geography-table loading or validation."""

    key_path: str | None
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class GeographyLoadResult:
    """The parsed geography table, or every error found while loading it."""

    geography: Geography | None = None
    document: Mapping[str, object] | None = None
    errors: tuple[GeographyError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.geography is not None


class _DuplicateKeyError(yaml.YAMLError):
    pass


class GeographyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys."""


GeographyLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: GeographyLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


GeographyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


_FIX: Final = "Edit tools/courtmap/geography.yaml; consult docs/archi/tools.md."
_ROOT_KEYS: Final = ("states", "federal", "state_courts", "unplaced", "corrections")
_STATE_KEYS: Final = ("name", "circuit")
_CORRECTION_KEYS: Final = ("code", "reason")
_FEDERAL_NULL_IDS: Final = ("scotus", *CIRCUITS)


def _path(prefix: str, key: object) -> str:
    rendered = str(key)
    return f"{prefix}.{rendered}" if prefix else rendered


def _nearest(path: str, valid: Sequence[str]) -> str:
    return difflib.get_close_matches(path, list(valid), n=1, cutoff=0.0)[0]


def _unknown(path: str, valid: Sequence[str]) -> GeographyError:
    return GeographyError(
        key_path=path,
        problem=(
            f"Unknown key '{path}'; nearest valid key is '{_nearest(path, valid)}'."
        ),
        fix=_FIX,
    )


def _error(path: str, detail: str) -> GeographyError:
    return GeographyError(
        key_path=path,
        problem=f"Invalid value for '{path}': {detail}.",
        fix=_FIX,
    )


def _walk_unknown(
    value: object,
    keys: Sequence[str],
    prefix: str,
    errors: list[GeographyError],
) -> None:
    if not isinstance(value, Mapping):
        return
    for key in value:
        path = _path(prefix, key)
        if not isinstance(key, str) or key not in keys:
            errors.append(_unknown(path, keys))


def _lookup(document: Mapping[str, object], key: str) -> tuple[bool, object]:
    if key not in document:
        return False, None
    return True, document[key]


def _required(
    value: Mapping[str, object],
    key: str,
    path: str,
    errors: list[GeographyError],
) -> tuple[bool, object]:
    """Return whether *key* is present and its value; a present null is a value."""

    if key not in value:
        errors.append(_error(path, "missing required key"))
        return False, None
    return True, value[key]


def _string(
    value: Mapping[str, object],
    key: str,
    path: str,
    errors: list[GeographyError],
) -> str | None:
    present, item = _required(value, key, path, errors)
    if not present:
        return None
    if not isinstance(item, str) or not item:
        errors.append(_error(path, f"expected a non-empty string (got {item!r})"))
        return None
    return item


def _validate_states(value: object, errors: list[GeographyError]) -> None:
    if not isinstance(value, Mapping):
        errors.append(_error("states", f"expected a mapping (got {value!r})"))
        return
    states = cast(Mapping[object, object], value)
    _walk_unknown(states, STATE_CODES, "states", errors)
    for code in STATE_CODES:
        if code not in states:
            errors.append(_error(f"states.{code}", "missing required state"))
    for state_code, entry in states.items():
        path = _path("states", state_code)
        if not isinstance(state_code, str) or state_code not in STATE_CODES:
            continue
        if not isinstance(entry, Mapping):
            errors.append(_error(path, f"expected a mapping (got {entry!r})"))
            continue
        state = cast(Mapping[str, object], entry)
        _walk_unknown(state, _STATE_KEYS, path, errors)
        _string(state, "name", f"{path}.name", errors)
        present, circuit = _required(state, "circuit", f"{path}.circuit", errors)
        if present and circuit is not None and (
            not isinstance(circuit, str) or circuit not in CIRCUITS
        ):
            errors.append(
                _error(
                    f"{path}.circuit",
                    f"expected a circuit id or null (got {circuit!r})",
                )
            )


def _validate_federal(value: object, errors: list[GeographyError]) -> None:
    if not isinstance(value, Mapping):
        errors.append(_error("federal", f"expected a mapping (got {value!r})"))
        return
    federal = cast(Mapping[object, object], value)
    for identifier in _FEDERAL_NULL_IDS:
        if identifier not in federal:
            errors.append(_error(f"federal.{identifier}", "missing required federal court"))
    for federal_id, state in federal.items():
        path = _path("federal", federal_id)
        if not isinstance(federal_id, str) or not federal_id:
            errors.append(_error(path, f"expected a non-empty string id (got {federal_id!r})"))
            continue
        if federal_id in _FEDERAL_NULL_IDS:
            if state is not None:
                errors.append(_error(path, f"expected null for {federal_id!r} (got {state!r})"))
        elif not isinstance(state, str) or state not in STATE_CODES:
            errors.append(_error(path, f"expected a district state code (got {state!r})"))


def _validate_state_courts(value: object, errors: list[GeographyError]) -> None:
    if not isinstance(value, Mapping):
        errors.append(_error("state_courts", f"expected a mapping (got {value!r})"))
        return
    for identifier, state in cast(Mapping[object, object], value).items():
        path = _path("state_courts", identifier)
        if not isinstance(identifier, str) or not identifier:
            errors.append(_error(path, f"expected a non-empty string id (got {identifier!r})"))
        if not isinstance(state, str) or state not in STATE_CODES:
            errors.append(_error(path, f"expected a state code (got {state!r})"))


def _validate_unplaced(value: object, errors: list[GeographyError]) -> None:
    if not isinstance(value, Mapping):
        errors.append(_error("unplaced", f"expected a mapping (got {value!r})"))
        return
    for identifier, reason in cast(Mapping[object, object], value).items():
        path = _path("unplaced", identifier)
        if not isinstance(identifier, str) or not identifier:
            errors.append(_error(path, f"expected a non-empty string id (got {identifier!r})"))
        if not isinstance(reason, str) or not reason:
            errors.append(_error(path, f"expected a non-empty reason (got {reason!r})"))


def _validate_corrections(value: object, errors: list[GeographyError]) -> None:
    if not isinstance(value, Mapping):
        errors.append(_error("corrections", f"expected a mapping (got {value!r})"))
        return
    for identifier, entry in cast(Mapping[object, object], value).items():
        path = _path("corrections", identifier)
        if not isinstance(identifier, str) or not identifier:
            errors.append(_error(path, f"expected a non-empty string id (got {identifier!r})"))
        if not isinstance(entry, Mapping):
            errors.append(_error(path, f"expected a mapping (got {entry!r})"))
            continue
        correction = cast(Mapping[str, object], entry)
        _walk_unknown(correction, _CORRECTION_KEYS, path, errors)
        code = _string(correction, "code", f"{path}.code", errors)
        _string(correction, "reason", f"{path}.reason", errors)
        if code is not None and code not in CSV_CODES:
            errors.append(
                _error(
                    f"{path}.code",
                    f"expected a CourtListener jurisdiction code, nearest '{_nearest(code, CSV_CODES)}' (got {code!r})",
                )
            )


def _validate_placement_exclusivity(
    document: Mapping[str, object], errors: list[GeographyError]
) -> None:
    placements: dict[str, set[object]] = {}
    for section in ("federal", "state_courts", "unplaced"):
        value = document.get(section)
        if isinstance(value, Mapping):
            placements[section] = set(value)
    sections = tuple(placements)
    for index, first in enumerate(sections):
        for second in sections[index + 1 :]:
            for identifier in sorted(placements[first] & placements[second], key=str):
                errors.append(
                    _error(
                        f"{first}.{identifier}",
                        f"court id {identifier!r} appears in both {first} and {second} placement sections",
                    )
                )


def validate_geography(document: Mapping[str, object]) -> list[GeographyError]:
    """Return all shape, key, and vocabulary errors in a geography table."""

    errors: list[GeographyError] = []
    _walk_unknown(document, _ROOT_KEYS, "", errors)
    values: dict[str, object] = {}
    for section in _ROOT_KEYS:
        present, value = _lookup(document, section)
        if not present:
            errors.append(_error(section, "missing required section"))
        else:
            values[section] = value
    if "states" in values:
        _validate_states(values["states"], errors)
    if "federal" in values:
        _validate_federal(values["federal"], errors)
    if "state_courts" in values:
        _validate_state_courts(values["state_courts"], errors)
    if "unplaced" in values:
        _validate_unplaced(values["unplaced"], errors)
    if "corrections" in values:
        _validate_corrections(values["corrections"], errors)
    _validate_placement_exclusivity(values, errors)
    return errors


def _construct(document: Mapping[str, object]) -> Geography:
    states_value = cast(Mapping[str, Mapping[str, object]], document["states"])
    states = {
        code: State(
            name=cast(str, entry["name"]),
            circuit=cast(str | None, entry["circuit"]),
        )
        for code, entry in states_value.items()
    }
    federal = {
        identifier: cast(str | None, state)
        for identifier, state in cast(Mapping[str, object], document["federal"]).items()
    }
    state_courts = {
        identifier: cast(str, state)
        for identifier, state in cast(Mapping[str, object], document["state_courts"]).items()
    }
    unplaced = {
        identifier: cast(str, reason)
        for identifier, reason in cast(Mapping[str, object], document["unplaced"]).items()
    }
    corrections_value = cast(Mapping[str, Mapping[str, object]], document["corrections"])
    corrections = {
        identifier: Correction(
            code=cast(str, entry["code"]),
            reason=cast(str, entry["reason"]),
        )
        for identifier, entry in corrections_value.items()
    }
    return Geography(states, federal, state_courts, unplaced, corrections)


def read_geography(
    path: PathLike, *, host: Host | None = None
) -> GeographyLoadResult:
    """Read and parse a geography table without constructing its dataclasses."""

    io = host or RealHost()
    try:
        text = io.read_text(path)
    except FileNotFoundError:
        return GeographyLoadResult(errors=(GeographyError(None, f"geography table is missing: {path}", _FIX),))
    except PermissionError:
        return GeographyLoadResult(
            errors=(GeographyError(None, f"geography table is unreadable due to permissions: {path}", _FIX),)
        )
    except UnicodeDecodeError:
        return GeographyLoadResult(errors=(GeographyError(None, f"geography table is not valid UTF-8: {path}", _FIX),))
    except OSError as exc:
        return GeographyLoadResult(errors=(GeographyError(None, f"geography table is unreadable: {path} ({exc})", _FIX),))

    try:
        document = yaml.load(text, Loader=GeographyLoader)
    except _DuplicateKeyError as exc:
        return GeographyLoadResult(errors=(GeographyError(None, f"geography table has a {exc}", _FIX),))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return GeographyLoadResult(
            errors=(GeographyError(None, f"geography table has a YAML parse error{location}: {exc}", _FIX),)
        )
    if document is None:
        return GeographyLoadResult(errors=(GeographyError(None, "geography table is empty", _FIX),))
    if not isinstance(document, Mapping):
        return GeographyLoadResult(errors=(GeographyError(None, "geography table root must be a mapping", _FIX),))
    return GeographyLoadResult(document=cast(Mapping[str, object], document))


def load_geography(
    path: PathLike, *, host: Host | None = None
) -> GeographyLoadResult:
    """Parse, validate, and construct the hand-authored geography table."""

    result = read_geography(path, host=host)
    if result.errors or result.document is None:
        return result
    errors = validate_geography(result.document)
    if errors:
        return GeographyLoadResult(document=result.document, errors=tuple(errors))
    return GeographyLoadResult(
        geography=_construct(result.document), document=result.document
    )


def render_errors(errors: Sequence[GeographyError]) -> str:
    """Render one refusal per line, with the corrective action last."""

    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )
