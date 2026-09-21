"""Read the content-free ranked-list JSONL file used by ``judgments@1``."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.evaluation.evalset import JUDGMENT_ID_PATTERN, Finding
from gideon.evaluation.judgments import coordinate_findings
from gideon.evaluation.rankmetrics import Coordinates

RANKED_KEYS: Final[tuple[str, ...]] = ("query_id", "ranked")
ENTRY_KEYS: Final[tuple[str, ...]] = ("source_id", "sha256", "start", "end")
RANKED_FIX: Final[str] = "Correct the ranked-list JSONL file, then retry."
"""Every ranked-list finding's fix, and the refusing stage row's (``command``)."""
RANKED_LABEL: Final[str] = "ranked-list"
"""Every finding's file label, in place of the path.

The ranked file may live anywhere readable and is never committed, so its
directory can name a client or a matter; the path is used for the read alone
and never reaches a stream or a row. What locates a finding is the line, the
query id, and the entry ordinal, and what identifies the file on the record is
its SHA-256 (Data Discipline, §19.4).
"""


@dataclass(frozen=True, slots=True)
class RankedReadResult:
    """The ranked lists and file digest, or every content-free finding."""

    ranked: Mapping[str, tuple[Coordinates, ...]] | None = None
    sha256: str | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the ranked-list file was read without a finding."""

        return self.ranked is not None and self.sha256 is not None and not self.findings


def _finding(
    file: str,
    query_id: str | None,
    line: int,
    rule: str,
    ordinal: int | None = None,
) -> Finding:
    """Build a ranked-list finding located by line, query id, and ordinal.

    ``Finding.text`` renders an ``id`` in place of the line, so the location
    goes in the rule and the id stays ``None``: a ranked finding names all
    three, as the pool reader's does. A query id that is not of the judgments
    pattern locates nothing and is left out rather than echoed.
    """

    located = (
        query_id
        if query_id is not None and JUDGMENT_ID_PATTERN.fullmatch(query_id) is not None
        else None
    )
    location = "" if located is None else f"query {located}"
    if ordinal is not None:
        location = f"{location} ranked {ordinal}".strip()
    return Finding(file, None, line, f"{location}: {rule}" if location else rule, RANKED_FIX)


def _ordered_findings(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Return findings in file, line, and rule order."""

    return tuple(sorted(findings, key=lambda item: (item.file, item.line or 0, item.rule)))


def read(path: str | Path, allowed_query_ids: Iterable[str]) -> RankedReadResult:
    """Read a ranked-list file for the supplied active query ids."""

    file_path = Path(path)
    file = RANKED_LABEL
    try:
        data = file_path.read_bytes()
    except OSError:
        return RankedReadResult(findings=(_finding(file, None, 0, "file cannot be read"),))

    digest = hashlib.sha256(data).hexdigest()
    findings: list[Finding] = []
    if data and not data.endswith(b"\n"):
        findings.append(_finding(file, None, 0, "file has no final newline"))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(_finding(file, None, 1, "file is not UTF-8"))
        return RankedReadResult(findings=_ordered_findings(findings))

    allowed = frozenset(allowed_query_ids)
    seen_queries: set[str] = set()
    ranked: dict[str, tuple[Coordinates, ...]] = {}
    for line, line_text in enumerate(text.splitlines(), start=1):
        try:
            value = json.loads(line_text)
        except json.JSONDecodeError:
            findings.append(_finding(file, None, line, "line is not one JSON object"))
            continue
        if not isinstance(value, dict):
            findings.append(_finding(file, None, line, "line is not one JSON object"))
            continue

        query_value = value.get("query_id")
        query_id = query_value if isinstance(query_value, str) else None
        line_findings: list[Finding] = []
        if tuple(value) != RANKED_KEYS:
            line_findings.append(_finding(file, query_id, line, "keys or order"))
        query_valid = (
            query_id is not None and JUDGMENT_ID_PATTERN.fullmatch(query_id) is not None
        )
        if not query_valid:
            line_findings.append(_finding(file, query_id, line, "query_id pattern"))
        elif query_id not in allowed:
            line_findings.append(_finding(file, query_id, line, "query_id is not allowed"))
        elif query_id in seen_queries:
            line_findings.append(_finding(file, query_id, line, "query_id appears more than once"))
        else:
            seen_queries.add(query_id)

        ranked_value = value.get("ranked")
        coordinates: list[Coordinates] = []
        seen_coordinates: set[Coordinates] = set()
        if not isinstance(ranked_value, list):
            line_findings.append(_finding(file, query_id, line, "ranked is not a list"))
        else:
            for ordinal, entry in enumerate(ranked_value, start=1):
                if not isinstance(entry, dict):
                    line_findings.append(
                        _finding(file, query_id, line, "entry is not one JSON object", ordinal)
                    )
                    continue
                entry_findings: list[Finding] = []
                if tuple(entry) != ENTRY_KEYS:
                    entry_findings.append(
                        _finding(file, query_id, line, "keys or order", ordinal)
                    )
                entry_findings.extend(
                    _finding(file, query_id, line, finding.rule, ordinal)
                    for finding in coordinate_findings(entry, file, line)
                )
                if not entry_findings:
                    coordinate = (
                        entry["source_id"],
                        entry["sha256"],
                        entry["start"],
                        entry["end"],
                    )
                    if coordinate in seen_coordinates:
                        entry_findings.append(
                            _finding(
                                file,
                                query_id,
                                line,
                                "coordinates appear more than once",
                                ordinal,
                            )
                        )
                    else:
                        seen_coordinates.add(coordinate)
                        coordinates.append(coordinate)
                line_findings.extend(entry_findings)

        findings.extend(line_findings)
        if not line_findings and query_id is not None:
            ranked[query_id] = tuple(coordinates)

    ordered = _ordered_findings(findings)
    if ordered:
        return RankedReadResult(findings=ordered)
    return RankedReadResult(ranked=ranked, sha256=digest)
