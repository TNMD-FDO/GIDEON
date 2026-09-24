"""Loader and query model for the committed CourtListener court map."""

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import yaml  # type: ignore[import-untyped]

from gideon.host.sysio import Host, PathLike, RealHost

LEVELS: Final = (
    "scotus",
    "circuit",
    "district",
    "state_supreme",
    "state_appellate",
    "other",
)
"""The closed set of court levels in the release artifact."""

CIRCUITS: Final = (
    "ca1",
    "ca2",
    "ca3",
    "ca4",
    "ca5",
    "ca6",
    "ca7",
    "ca8",
    "ca9",
    "ca10",
    "ca11",
    "cadc",
    "cafc",
)
"""The thirteen federal courts of appeals identifiers."""

STATES: Final = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
)
"""The fifty states and the District of Columbia; each has a court of last resort."""

TERRITORIES: Final = ("AS", "GU", "MP", "PR", "VI")
"""The five territories are placed like states; not all have a district."""

STATE_CODES: Final = (*STATES, *TERRITORIES)
"""Every USPS code a court's ``state`` may carry."""

SOURCE_FILE: Final = re.compile(r"^courts-(\d{4}-\d{2}-\d{2})\.csv\.bz2$")
"""The dated name of the CourtListener bulk file a map records; group 1 is the date."""


@dataclass(frozen=True, slots=True)
class Court:
    """One CourtListener court and its release-owned geography."""

    id: str
    circuit: str | None
    state: str | None
    level: str
    name: str


@dataclass(frozen=True, slots=True)
class CourtSource:
    """The source metadata recorded in a generated court map."""

    file: str
    date: str
    sha256: str
    rows: int
    levels: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class CourtMap:
    """The release court map and the queries its readers share."""

    source: CourtSource
    courts: Mapping[str, Court]

    def court(self, identifier: str) -> Court | None:
        """Return the court for *identifier*, or ``None`` when absent."""

        return self.courts.get(identifier)

    def ids_at_level(self, level: str) -> tuple[str, ...]:
        """Return the sorted identifiers at *level*."""

        return tuple(
            sorted(court.id for court in self.courts.values() if court.level == level)
        )

    def nearest_id(self, identifier: str, level: str) -> str:
        """Return the closest identifier at *level*, always choosing one."""

        candidates = self.ids_at_level(level)
        if not candidates:
            raise ValueError(f"court map has no courts at level {level!r}")
        return difflib.get_close_matches(identifier, list(candidates), n=1, cutoff=0.0)[0]

    def unresolved(self, identifiers: Iterable[str]) -> tuple[str, ...]:
        """Return the identifiers in *identifiers* that are absent from the map."""

        return tuple(identifier for identifier in identifiers if identifier not in self.courts)

    def appellate_courts(self, state: str) -> tuple[str, ...]:
        """Return state-level court identifiers sharing the USPS *state* code."""

        return tuple(
            sorted(
                court.id
                for court in self.courts.values()
                if court.state == state
                and court.level in ("state_supreme", "state_appellate")
            )
        )


@dataclass(frozen=True, slots=True)
class CourtsError:
    """A structured refusal from court-map loading or validation."""

    key_path: str | None
    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class CourtMapLoadResult:
    """The parsed court map, or every error found while loading it."""

    court_map: CourtMap | None = None
    document: Mapping[str, object] | None = None
    errors: tuple[CourtsError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.court_map is not None


class _DuplicateKeyError(yaml.YAMLError):
    pass


class CourtMapLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys."""


CourtMapLoader.yaml_implicit_resolvers = {
    initial: list(resolvers)
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: CourtMapLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


CourtMapLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


_FIX: Final = (
    "Restore courts.yaml from the release checkout; consult docs/runbooks/release-files.md §6."
)
_ROOT_KEYS: Final = ("source", "courts")
_SOURCE_KEYS: Final = ("file", "date", "sha256", "rows", "levels")
_COURT_KEYS: Final = ("circuit", "state", "level", "name")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


def _path(prefix: str, key: object) -> str:
    rendered = str(key)
    return f"{prefix}.{rendered}" if prefix else rendered


def _nearest(path: str, valid: Sequence[str]) -> str:
    return difflib.get_close_matches(path, list(valid), n=1, cutoff=0.0)[0]


def _unknown(path: str, valid: Sequence[str]) -> CourtsError:
    return CourtsError(
        key_path=path,
        problem=(
            f"Unknown key '{path}'; nearest valid key is '{_nearest(path, valid)}'."
        ),
        fix=_FIX,
    )


def _error(path: str, detail: str) -> CourtsError:
    return CourtsError(
        key_path=path,
        problem=f"Invalid value for '{path}': {detail}.",
        fix=_FIX,
    )


def _walk_unknown(
    value: object,
    keys: Sequence[str],
    prefix: str,
    errors: list[CourtsError],
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


def _required(
    value: Mapping[str, object],
    key: str,
    path: str,
    errors: list[CourtsError],
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
    errors: list[CourtsError],
) -> str | None:
    present, item = _required(value, key, path, errors)
    if not present:
        return None
    if not isinstance(item, str) or not item:
        errors.append(_error(path, f"expected a non-empty string (got {item!r})"))
        return None
    return item


def _integer(
    value: Mapping[str, object],
    key: str,
    path: str,
    errors: list[CourtsError],
) -> int | None:
    present, item = _required(value, key, path, errors)
    if not present:
        return None
    if isinstance(item, bool) or not isinstance(item, int):
        errors.append(_error(path, f"expected an integer (got {item!r})"))
        return None
    return item


def _optional_string(
    value: Mapping[str, object],
    key: str,
    path: str,
    valid: Sequence[str],
    errors: list[CourtsError],
) -> str | None:
    present, item = _required(value, key, path, errors)
    if not present or item is None:
        return None
    if not isinstance(item, str) or item not in valid:
        errors.append(_error(path, f"expected one of {list(valid)} or null (got {item!r})"))
        return None
    return item


def _validate_source(
    source: object,
    courts: object,
    errors: list[CourtsError],
) -> None:
    if not isinstance(source, Mapping):
        errors.append(_error("source", f"expected a mapping (got {source!r})"))
        return
    source_value = cast(Mapping[str, object], source)
    _walk_unknown(source_value, _SOURCE_KEYS, "source", errors)
    source_file = _string(source_value, "file", "source.file", errors)
    source_date = _string(source_value, "date", "source.date", errors)
    source_hash = _string(source_value, "sha256", "source.sha256", errors)
    source_rows = _integer(source_value, "rows", "source.rows", errors)
    levels_present, levels = _required(source_value, "levels", "source.levels", errors)

    date_match = SOURCE_FILE.fullmatch(source_file or "")
    if source_file is not None and date_match is None:
        errors.append(
            _error(
                "source.file",
                f"expected courts-YYYY-MM-DD.csv.bz2 (got {source_file!r})",
            )
        )
    if source_date is not None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
            errors.append(_error("source.date", f"expected YYYY-MM-DD (got {source_date!r})"))
        elif date_match is not None and source_date != date_match.group(1):
            errors.append(
                _error(
                    "source.date",
                    f"must agree with source.file (got {source_date!r})",
                )
            )
    if source_hash is not None and _SHA256.fullmatch(source_hash) is None:
        errors.append(
            _error(
                "source.sha256",
                "expected 64 lowercase hexadecimal characters",
            )
        )
    if source_rows is not None and source_rows < 0:
        errors.append(_error("source.rows", f"expected a non-negative integer (got {source_rows!r})"))

    if not levels_present:
        pass
    elif not isinstance(levels, Mapping):
        errors.append(_error("source.levels", f"expected a mapping (got {levels!r})"))
    else:
        levels_value = cast(Mapping[object, object], levels)
        _walk_unknown(levels_value, LEVELS, "source.levels", errors)
        for level in LEVELS:
            if level not in levels_value:
                errors.append(_error(f"source.levels.{level}", "missing required key"))
            else:
                count = levels_value[level]
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    errors.append(
                        _error(
                            f"source.levels.{level}",
                            f"expected a non-negative integer (got {count!r})",
                        )
                    )

        if isinstance(courts, Mapping):
            counts = dict.fromkeys(LEVELS, 0)
            for entry in courts.values():
                if isinstance(entry, Mapping):
                    entry_level = entry.get("level")
                    if isinstance(entry_level, str) and entry_level in counts:
                        counts[entry_level] += 1
            for level in LEVELS:
                count = levels_value.get(level)
                if (
                    isinstance(count, int)
                    and not isinstance(count, bool)
                    and count >= 0
                    and count != counts[level]
                ):
                    errors.append(
                        _error(
                            f"source.levels.{level}",
                            f"must equal the number of {level} courts ({counts[level]})",
                        )
                    )

    if source_rows is not None and isinstance(courts, Mapping) and source_rows != len(courts):
        errors.append(
            _error(
                "source.rows",
                f"must equal the number of courts ({len(courts)})",
            )
        )


def _validate_court(
    identifier: object,
    entry: object,
    errors: list[CourtsError],
) -> None:
    path = _path("courts", identifier)
    if not isinstance(identifier, str) or not identifier:
        errors.append(_error(path, f"expected a non-empty string id (got {identifier!r})"))
    if not isinstance(entry, Mapping):
        errors.append(_error(path, f"expected a mapping (got {entry!r})"))
        return
    court = cast(Mapping[str, object], entry)
    _walk_unknown(court, _COURT_KEYS, path, errors)
    circuit = _optional_string(court, "circuit", f"{path}.circuit", CIRCUITS, errors)
    state = _optional_string(court, "state", f"{path}.state", STATE_CODES, errors)
    level = _string(court, "level", f"{path}.level", errors)
    name = _string(court, "name", f"{path}.name", errors)
    if level is None or level not in LEVELS:
        if level is not None:
            errors.append(_error(f"{path}.level", f"expected one of {list(LEVELS)} (got {level!r})"))
        return
    if name is None or not isinstance(identifier, str) or not identifier:
        return
    if level == "scotus":
        if circuit is not None:
            errors.append(_error(f"{path}.circuit", "must be null for a scotus court"))
        if state is not None:
            errors.append(_error(f"{path}.state", "must be null for a scotus court"))
    elif level == "circuit":
        if circuit != identifier:
            errors.append(_error(f"{path}.circuit", f"must equal the court id {identifier!r}"))
        if state is not None:
            errors.append(_error(f"{path}.state", "must be null for a circuit court"))
    elif level == "district":
        if circuit is None:
            errors.append(_error(f"{path}.circuit", "must be set for a district court"))
        if state is None:
            errors.append(_error(f"{path}.state", "must be set for a district court"))
    elif level in ("state_supreme", "state_appellate") and state is None:
        errors.append(_error(f"{path}.state", "must be set for a state court"))
    elif level == "other":
        if circuit is not None:
            errors.append(_error(f"{path}.circuit", "must be null for an other court"))
        if state is not None:
            errors.append(_error(f"{path}.state", "must be null for an other court"))


def validate_court_map(document: Mapping[str, object]) -> list[CourtsError]:
    """Return all shape, vocabulary, count, and coherence errors in a map."""

    errors: list[CourtsError] = []
    _walk_unknown(document, _ROOT_KEYS, "", errors)
    source_present, source = _lookup(document, "source")
    courts_present, courts = _lookup(document, "courts")
    if not source_present:
        errors.append(_error("source", "missing required key"))
    if not courts_present:
        errors.append(_error("courts", "missing required key"))
    if not source_present or not courts_present:
        return errors
    if not isinstance(courts, Mapping):
        errors.append(_error("courts", f"expected a mapping (got {courts!r})"))
    else:
        for identifier, entry in courts.items():
            _validate_court(identifier, entry, errors)
    _validate_source(source, courts, errors)
    return errors


def _construct(document: Mapping[str, object]) -> CourtMap:
    source_value = cast(Mapping[str, object], document["source"])
    levels_value = cast(Mapping[str, int], source_value["levels"])
    courts_value = cast(Mapping[str, Mapping[str, object]], document["courts"])
    courts = {
        identifier: Court(
            id=identifier,
            circuit=cast(str | None, entry["circuit"]),
            state=cast(str | None, entry["state"]),
            level=cast(str, entry["level"]),
            name=cast(str, entry["name"]),
        )
        for identifier, entry in courts_value.items()
    }
    return CourtMap(
        source=CourtSource(
            file=cast(str, source_value["file"]),
            date=cast(str, source_value["date"]),
            sha256=cast(str, source_value["sha256"]),
            rows=cast(int, source_value["rows"]),
            levels=dict(levels_value),
        ),
        courts=courts,
    )


def render_errors(errors: Sequence[CourtsError]) -> str:
    """Render one refusal per line, with the corrective action last."""

    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )


def read_court_map(
    path: PathLike, *, host: Host | None = None
) -> CourtMapLoadResult:
    """Read and parse a court-map artifact without constructing its dataclasses."""

    io = host or RealHost()
    try:
        text = io.read_text(path)
    except FileNotFoundError:
        return CourtMapLoadResult(
            errors=(CourtsError(None, f"court map is missing: {path}", _FIX),)
        )
    except PermissionError:
        return CourtMapLoadResult(
            errors=(
                CourtsError(
                    None,
                    f"court map is unreadable due to permissions: {path}",
                    _FIX,
                ),
            )
        )
    except UnicodeDecodeError:
        return CourtMapLoadResult(
            errors=(CourtsError(None, f"court map is not valid UTF-8: {path}", _FIX),)
        )
    except OSError as exc:
        return CourtMapLoadResult(
            errors=(CourtsError(None, f"court map is unreadable: {path} ({exc})", _FIX),)
        )

    return parse_court_map(text)


def parse_court_map(text: str) -> CourtMapLoadResult:
    """Parse court-map text into its document (no validation, no dataclasses)."""

    try:
        document = yaml.load(text, Loader=CourtMapLoader)
    except _DuplicateKeyError as exc:
        return CourtMapLoadResult(
            errors=(CourtsError(None, f"court map has a {exc}", _FIX),)
        )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return CourtMapLoadResult(
            errors=(
                CourtsError(
                    None,
                    f"court map has a YAML parse error{location}: {exc}",
                    _FIX,
                ),
            )
        )
    if document is None:
        return CourtMapLoadResult(errors=(CourtsError(None, "court map is empty", _FIX),))
    if not isinstance(document, Mapping):
        return CourtMapLoadResult(
            errors=(CourtsError(None, "court map root must be a mapping", _FIX),)
        )
    return CourtMapLoadResult(document=cast(Mapping[str, object], document))


def _finish(result: CourtMapLoadResult) -> CourtMapLoadResult:
    if result.errors or result.document is None:
        return result
    errors = validate_court_map(result.document)
    if errors:
        return CourtMapLoadResult(document=result.document, errors=tuple(errors))
    return CourtMapLoadResult(
        court_map=_construct(result.document), document=result.document
    )


def load_court_map(
    path: PathLike, *, host: Host | None = None
) -> CourtMapLoadResult:
    """Parse, validate, and construct a court map."""

    return _finish(read_court_map(path, host=host))


def load_court_map_text(text: str) -> CourtMapLoadResult:
    """Parse, validate, and construct a court map from text already in hand.

    The generator loads its own output this way before writing it, so no
    text reaches the checkout that the product's loader reads differently.
    """

    return _finish(parse_court_map(text))


def default_courts_path() -> Path:
    """Return the committed court-map artifact path for this checkout."""

    return Path(__file__).parents[2] / "courts.yaml"
