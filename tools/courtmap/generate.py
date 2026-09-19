"""Pure court-map join and deterministic artifact writer."""

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from gideon.host.courts import CIRCUITS, LEVELS, STATES, Court, CourtMap, CourtSource
from tools.courtmap.geography import LEVEL_BY_CODE, Geography

SOURCE_URL: Final = (
    "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/"
    "bulk-data/courts-YYYY-MM-DD.csv.bz2"
)
"""The public object-name form; this tool never fetches it."""


@dataclass(frozen=True, slots=True)
class GenerationError:
    """A refusal from the pure geography-to-map join."""

    problem: str
    fix: str


@dataclass(frozen=True, slots=True)
class _CsvCourt:
    id: str
    code: str
    name: str


def _fix(section: str) -> str:
    return (
        f"Edit tools/courtmap/geography.yaml section '{section}'; then re-run "
        "python3 -m tools.courtmap."
    )


def _error(problem: str, section: str) -> GenerationError:
    return GenerationError(problem, _fix(section))


def _corrected_code(identifier: str, code: str, geography: Geography) -> str:
    correction = geography.corrections.get(identifier)
    return correction.code if correction is not None else code


def _read_rows(
    rows: Iterable[Mapping[str, object]], errors: list[GenerationError]
) -> dict[str, _CsvCourt]:
    by_id: dict[str, _CsvCourt] = {}
    for row_number, row in enumerate(rows, start=2):
        identifier = row.get("id")
        code = row.get("jurisdiction")
        name = row.get("full_name")
        if not isinstance(identifier, str) or not identifier:
            errors.append(
                GenerationError(
                    f"CSV row {row_number} has an empty court id.",
                    "Correct the CSV source, then re-run python3 -m tools.courtmap.",
                )
            )
            continue
        if not isinstance(name, str) or not name:
            errors.append(
                GenerationError(
                    f"CSV row {row_number} for court {identifier!r} has an empty full_name.",
                    "Correct the CSV source, then re-run python3 -m tools.courtmap.",
                )
            )
            continue
        if not isinstance(code, str):
            code = ""
        if identifier in by_id:
            errors.append(
                GenerationError(
                    f"CSV contains duplicate court id {identifier!r}.",
                    "Correct the CSV source, then re-run python3 -m tools.courtmap.",
                )
            )
            continue
        by_id[identifier] = _CsvCourt(identifier, code, name)
    return by_id


def _check_table_ids(
    rows: Mapping[str, _CsvCourt], geography: Geography, errors: list[GenerationError]
) -> None:
    for section, identifiers in (
        ("federal", geography.federal),
        ("state_courts", geography.state_courts),
        ("unplaced", geography.unplaced),
        ("corrections", geography.corrections),
    ):
        for identifier in sorted(identifiers):
            if identifier not in rows:
                errors.append(
                    _error(
                        f"{section} names court id {identifier!r}, but the CSV does not contain it",
                        section,
                    )
                )


def _check_federal_codes(
    rows: Mapping[str, _CsvCourt], geography: Geography, errors: list[GenerationError]
) -> None:
    for identifier in sorted(geography.federal):
        row = rows.get(identifier)
        if row is None:
            continue
        expected = "F" if identifier == "scotus" or identifier in CIRCUITS else "FD"
        if row.code != expected:
            errors.append(
                _error(
                    f"federal court {identifier!r} has CSV code {row.code!r}; expected {expected!r}",
                    "federal",
                )
            )


def _check_corrections(
    rows: Mapping[str, _CsvCourt], geography: Geography, errors: list[GenerationError]
) -> None:
    for identifier, correction in sorted(geography.corrections.items()):
        row = rows.get(identifier)
        if row is None:
            continue
        if row.code == correction.code:
            errors.append(
                _error(
                    f"correction for {identifier!r} does not change CSV code {row.code!r}",
                    "corrections",
                )
            )


def _check_state_placements(
    rows: Mapping[str, _CsvCourt], geography: Geography, errors: list[GenerationError]
) -> None:
    for identifier, row in sorted(rows.items()):
        corrected = _corrected_code(identifier, row.code, geography)
        if corrected not in LEVEL_BY_CODE:
            continue
        if identifier not in geography.state_courts and identifier not in geography.unplaced:
            errors.append(
                _error(
                    f"state-level CSV row {identifier!r} is absent from state_courts and unplaced",
                    "state_courts or unplaced",
                )
            )
    for identifier in sorted(geography.state_courts):
        state_court_row = rows.get(identifier)
        if state_court_row is None:
            continue
        corrected = _corrected_code(identifier, state_court_row.code, geography)
        if corrected not in LEVEL_BY_CODE:
            errors.append(
                _error(
                    f"state_courts row {identifier!r} has corrected CSV code {corrected!r}, not a state-level code",
                    "state_courts",
                )
            )
    for identifier in sorted(geography.unplaced):
        unplaced_row = rows.get(identifier)
        if unplaced_row is None:
            continue
        corrected = _corrected_code(identifier, unplaced_row.code, geography)
        if corrected not in LEVEL_BY_CODE:
            errors.append(
                _error(
                    f"unplaced row {identifier!r} has corrected CSV code {corrected!r}, not a state-level code",
                    "unplaced",
                )
            )


def _court_for_row(row: _CsvCourt, geography: Geography) -> Court:
    identifier = row.id
    if identifier in geography.federal:
        state = geography.federal[identifier]
        if identifier == "scotus":
            return Court(identifier, None, None, "scotus", row.name)
        if identifier in CIRCUITS:
            return Court(identifier, identifier, None, "circuit", row.name)
        assert state is not None
        return Court(identifier, geography.states[state].circuit, state, "district", row.name)
    if identifier in geography.state_courts:
        state = geography.state_courts[identifier]
        level = LEVEL_BY_CODE[_corrected_code(identifier, row.code, geography)]
        return Court(identifier, geography.states[state].circuit, state, level, row.name)
    return Court(identifier, None, None, "other", row.name)


def _check_law_counts(courts: Mapping[str, Court], errors: list[GenerationError]) -> None:
    counts = Counter(court.level for court in courts.values())
    for level, expected in (("scotus", 1), ("circuit", 13), ("district", 94)):
        if counts[level] != expected:
            errors.append(
                _error(
                    f"law-fixed {level} count is {counts[level]}, expected {expected}",
                    "federal",
                )
            )


def _check_state_supreme(courts: Mapping[str, Court], errors: list[GenerationError]) -> None:
    required = set(STATES)
    present = {
        court.state
        for court in courts.values()
        if court.level == "state_supreme" and court.state is not None
    }
    for state in sorted(required - present):
        errors.append(
            _error(
                f"state {state!r} has no state_supreme court",
                "state_courts",
            )
        )


def generate(
    rows: Iterable[Mapping[str, object]],
    geography: Geography,
    *,
    file: str,
    date: str,
    sha256: str,
) -> CourtMap | tuple[GenerationError, ...]:
    """Join CSV rows and hand geography, returning a map or all refusals.

    *file*, *date*, and *sha256* name the compressed CSV the rows came from
    and are recorded in the map's ``source`` block beside the counts.
    """

    errors: list[GenerationError] = []
    csv_rows = _read_rows(rows, errors)
    _check_table_ids(csv_rows, geography, errors)
    _check_federal_codes(csv_rows, geography, errors)
    _check_corrections(csv_rows, geography, errors)
    _check_state_placements(csv_rows, geography, errors)
    if errors:
        return tuple(errors)

    courts = {identifier: _court_for_row(row, geography) for identifier, row in csv_rows.items()}
    _check_law_counts(courts, errors)
    _check_state_supreme(courts, errors)
    if errors:
        return tuple(errors)
    counts = Counter(court.level for court in courts.values())
    source = CourtSource(
        file=file,
        date=date,
        sha256=sha256,
        rows=len(courts),
        levels={level: counts[level] for level in LEVELS},
    )
    return CourtMap(source, courts)


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _nullable(value: str | None) -> str:
    return "null" if value is None else value


def render_court_map(court_map: CourtMap) -> str:
    """Render a court map as deterministic, reviewable YAML text."""

    lines = [
        "# Generated by python3 -m tools.courtmap; never hand-edit.",
        "# Source: CourtListener's bulk courts data (Free Law Project), joined to",
        "# tools/courtmap/geography.yaml. Object form: " + SOURCE_URL,
        "# Regenerate with: python3 -m tools.courtmap --csv <local courts-YYYY-MM-DD.csv.bz2>",
        "source:",
        f"  file: {_quoted(court_map.source.file)}",
        f"  date: {_quoted(court_map.source.date)}",
        f"  sha256: {_quoted(court_map.source.sha256)}",
        f"  rows: {court_map.source.rows}",
        "  levels:",
    ]
    lines.extend(
        f"    {level}: {court_map.source.levels[level]}" for level in LEVELS
    )
    lines.append("courts:")
    for identifier in sorted(court_map.courts):
        court = court_map.courts[identifier]
        lines.append(
            f"  {identifier}: {{circuit: {_nullable(court.circuit)}, "
            f"state: {_nullable(court.state)}, level: {court.level}, "
            f"name: {_quoted(court.name)}}}"
        )
    return "\n".join(lines) + "\n"


def render_errors(errors: Sequence[GenerationError]) -> str:
    """Render one generator refusal per line, with its fix last."""

    return "\n".join(
        f"{' '.join(error.problem.splitlines())} Fix: {error.fix}" for error in errors
    )
