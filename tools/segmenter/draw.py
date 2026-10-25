"""Draw frozen segmenter samples and checked successors from staged court opinions."""

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, TextIO, cast

from gideon.evaluation import evalset
from tools.segmenter import manifest
from tools.segmenter.manifest import CASE_PATH, IDS_PATH, MIRROR_IDS_PATH

DEFAULT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
REQUIRED_COURTS: Final[tuple[str, ...]] = ("ca6", "scotus")
SOURCE_COLUMNS: Final[tuple[str, ...]] = evalset.SEGMENTER_TEXT_SOURCES
CSV_OPTIONS: Final[Mapping[str, object]] = {
    "delimiter": ",", "quotechar": '"', "escapechar": "\\",
    "doublequote": False, "strict": True,
}
# The staged dump has opinion text fields measured in megabytes.
CSV_FIELD_LIMIT: Final[int] = sys.maxsize
CHECK_WINDOW: Final[int] = 64
# The stage the frozen draw was taken from, which a successor draw replays.
FROZEN_LABEL: Final[str] = "corpus-2026-10-07"
csv.field_size_limit(CSV_FIELD_LIMIT)


class DrawError(Exception):
    """A content-free refusal with a corrective action."""

    def __init__(self, stage: str, problem: str, fix: str) -> None:
        super().__init__(problem)
        self.stage = stage
        self.problem = problem
        self.fix = fix


@dataclass(frozen=True, slots=True)
class Sample:
    """One drawn opinion and its stratum."""

    opinion_id: int
    court: str
    decade: int
    text_source: str


Cell = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class Successor:
    """A retired case's replacement and its candidate check coordinates."""

    retired: str
    sample: Sample
    fallback: Cell | None
    rank: int
    checked: int


def _read_csv(path: Path, required: tuple[str, ...], *, stage: str = "stage") -> Iterator[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.reader(source, **CSV_OPTIONS)  # type: ignore[arg-type]
            header = next(reader)
            if len(set(header)) != len(header) or any(name not in header for name in required):
                raise ValueError("columns")
            for fields in reader:
                if len(fields) != len(header):
                    raise ValueError("width")
                yield dict(zip(header, fields, strict=True))
    except (OSError, UnicodeError, csv.Error, ValueError, StopIteration) as error:
        raise DrawError(stage, "staged CSV cannot be read", "Restore the staged court tables, then retry.") from error


def _stage(path: Path) -> tuple[str, str, Mapping[str, object]]:
    try:
        value = json.loads((path / "stage.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise DrawError("stage", "stage record cannot be read", "Provide a whole staged tranche, then retry.") from error
    if not isinstance(value, dict):
        raise DrawError("stage", "stage record is invalid", "Provide a whole staged tranche, then retry.")
    label, snapshot, courts, counts = (
        value.get("label"), value.get("snapshot"), value.get("courts"), value.get("counts"),
    )
    if (
        not isinstance(label, str) or not isinstance(snapshot, str)
        or not isinstance(courts, list) or any(not isinstance(court, str) for court in courts)
        or set(courts) != set(REQUIRED_COURTS)
        or not isinstance(counts, dict) or set(counts) != set(REQUIRED_COURTS)
        or any(not isinstance(counts.get(court), dict) or type(counts[court].get("opinions")) is not int
               or counts[court]["opinions"] < 0 for court in REQUIRED_COURTS)
    ):
        raise DrawError("stage", "stage record lacks a required court or count", "Provide the two-court staged tranche, then retry.")
    return label, snapshot, counts


def _population(
    path: Path, counts: Mapping[str, object]
) -> tuple[dict[Cell, list[int]], dict[str, int], dict[str, int]]:
    cells: dict[Cell, list[int]] = defaultdict(list)
    skipped: dict[str, int] = {}
    no_cluster: dict[str, int] = {}
    seen_ids: set[int] = set()
    for court in REQUIRED_COURTS:
        decades: dict[str, int] = {}
        for row in _read_csv(path / court / "opinion-clusters.csv", ("id", "date_filed"), stage="clusters"):
            try:
                filed = date.fromisoformat(row["date_filed"])
                if row["id"] in decades:
                    raise ValueError("duplicate")
            except ValueError as error:
                raise DrawError("clusters", "cluster date or id is invalid", "Restore the staged clusters, then retry.") from error
            decades[row["id"]] = filed.year // 10 * 10
        expected = counts[court]
        assert isinstance(expected, dict)
        opinion_rows = _read_csv(path / court / "opinions.csv", ("id", "cluster_id", *SOURCE_COLUMNS), stage="opinions")
        skipped[court] = 0
        no_cluster[court] = 0
        row_count = 0
        for row in opinion_rows:
            row_count += 1
            try:
                opinion_id = int(row["id"])
                if opinion_id <= 0 or opinion_id in seen_ids:
                    raise ValueError("id")
            except ValueError as error:
                raise DrawError("opinions", "opinion id is invalid", "Restore the staged opinions, then retry.") from error
            seen_ids.add(opinion_id)
            # An opinion with no dated cluster has no decade, so like a row with
            # no text it is counted and left out of the population.
            decade = decades.get(row["cluster_id"])
            if decade is None:
                no_cluster[court] += 1
                continue
            source = next((name for name in SOURCE_COLUMNS if row[name].strip()), None)
            if source is None:
                skipped[court] += 1
                continue
            cells[(court, source, decade)].append(opinion_id)
        if row_count != expected["opinions"]:
            raise DrawError("opinions", "staged opinion count differs from stage record", "Restore the staged opinions, then retry.")
    return dict(cells), skipped, no_cluster


def _largest_remainder[K: str | int](
    weights: Mapping[K, int], seats: int, *, start: Mapping[K, int] | None = None,
) -> dict[K, int]:
    """Allocate the seats *start* leaves in proportion to *weights*, each key
    capped at its weight, ties in the remainders broken by key."""

    keys = sorted(weights, key=str)
    assigned: dict[K, int] = {key: 0 if start is None else start.get(key, 0) for key in keys}
    if sum(assigned.values()) > seats or seats > sum(weights.values()):
        raise DrawError(
            "allocate", "a stratum's allocation is impossible under the rule",
            "Reduce --per-court or restore the staged rows.",
        )
    left = seats - sum(assigned.values())
    while left:
        eligible = [key for key in keys if assigned[key] < weights[key]]
        total_weight = sum(weights[key] for key in eligible)
        quotients = {key: left * weights[key] / total_weight for key in eligible}
        floors = {key: min(weights[key] - assigned[key], int(quotients[key])) for key in eligible}
        for key in eligible:
            assigned[key] += floors[key]
        left -= sum(floors.values())
        order = sorted(eligible, key=lambda key: (-(quotients[key] - int(quotients[key])), str(key)))
        for key in order:
            if left and assigned[key] < weights[key]:
                assigned[key] += 1
                left -= 1
    return assigned


def allocate(cells: Mapping[Cell, Sequence[int]], per_court: int) -> dict[Cell, int]:
    """Split each court equally by source, then proportionally by decade."""

    allocation: dict[Cell, int] = {}
    for court in REQUIRED_COURTS:
        source_counts = {
            source: sum(len(ids) for (cell_court, cell_source, _), ids in cells.items() if cell_court == court and cell_source == source)
            for source in SOURCE_COLUMNS
        }
        source_counts = {source: count for source, count in source_counts.items() if count}
        if not source_counts:
            raise DrawError("allocate", "court has no usable opinions", "Restore the staged opinion text, then retry.")
        # An equal share per source, a thin source taken whole; the shortfall
        # it leaves is spread over the others in proportion to their rows.
        sources = sorted(source_counts)
        base, extra = divmod(per_court, len(sources))
        even = {
            source: min(source_counts[source], base + (index < extra))
            for index, source in enumerate(sources)
        }
        source_seats = _largest_remainder(source_counts, per_court, start=even)
        for source, seats in source_seats.items():
            if seats == 0:
                continue
            decade_counts = {
                decade: len(ids) for (cell_court, cell_source, decade), ids in cells.items()
                if cell_court == court and cell_source == source
            }
            for decade, count in _largest_remainder(decade_counts, seats, start=dict.fromkeys(decade_counts, 1)).items():
                allocation[(court, source, decade)] = count
    return allocation


def _shuffled(
    cells: Mapping[Cell, Sequence[int]], allocation: Mapping[Cell, int], seed: int,
) -> Mapping[Cell, tuple[int, ...]]:
    randomizer = random.Random(seed)
    orders: dict[Cell, tuple[int, ...]] = {}
    # Keep the original draw's RNG sequence. Cells allocated no seats follow
    # that sequence so their fallback candidates cannot move the drawn prefix.
    for cell in (*sorted(allocation), *sorted(cells.keys() - allocation.keys())):
        ids = sorted(cells[cell])
        randomizer.shuffle(ids)
        orders[cell] = tuple(ids)
    return orders


def _draw(orders: Mapping[Cell, Sequence[int]], allocation: Mapping[Cell, int]) -> tuple[Sample, ...]:
    drawn: list[Sample] = []
    for court, source, decade in sorted(allocation):
        ids = orders[(court, source, decade)]
        drawn.extend(Sample(opinion_id, court, decade, source) for opinion_id in ids[:allocation[(court, source, decade)]])
    return tuple(sorted(drawn, key=lambda sample: (sample.court, sample.text_source, sample.decade, sample.opinion_id)))


def _json_line(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def _cell(case: Mapping[str, object]) -> Cell:
    return cast(str, case["court"]), cast(str, case["text_source"]), cast(int, case["decade"])


def _cell_row(cell: Cell) -> dict[str, object]:
    return {"court": cell[0], "text_source": cell[1], "decade": cell[2]}


def _load_set(set_root: Path) -> evalset.LoadedSet:
    result = evalset.load_set(set_root)
    if result.findings or result.loaded is None:
        fix = result.findings[0].fix if result.findings else "Restore the eval set, then retry."
        raise DrawError("retire", "eval set cannot be loaded", fix)
    return result.loaded


def _retired_cases(loaded: evalset.LoadedSet, requested: Sequence[str]) -> dict[str, evalset.Case]:
    listed = set(loaded.slices.get("segmenter-300", ()))
    superseded = {case.get("supersedes") for case in loaded.cases_by_id.values()}
    retired: dict[str, evalset.Case] = {}
    for case_id in requested:
        case = loaded.cases_by_id.get(case_id)
        if (
            evalset.SEGMENTER_ID_PATTERN.fullmatch(case_id) is None
            or case_id not in listed or case is None
            or (case.get("suite"), case.get("category")) != ("build-gates", "segmenter-300")
            or case_id in superseded or case_id in retired
        ):
            location = f" {case_id}" if evalset.SEGMENTER_ID_PATTERN.fullmatch(case_id) else ""
            raise DrawError(
                "retire", f"retired id{location} is unlisted, not segmenter, superseded, or repeated",
                "Name each listed, unsuperseded segmenter case once with --retire, then retry.",
            )
        retired[case_id] = case
    return dict(sorted(retired.items()))


def _successors(
    stage_root: Path, *, orders: Mapping[Cell, tuple[int, ...]], allocation: Mapping[Cell, int],
    drawn: Sequence[Sample], label: str, per_court: int, loaded: evalset.LoadedSet,
    retired: Mapping[str, evalset.Case],
) -> tuple[tuple[Successor, ...], dict[str, int]]:
    """Replay the draw and check ranked candidate windows with the ingest's parser."""

    listed = set(loaded.slices.get("segmenter-300", ()))
    frozen = [case for case in loaded.cases_by_file.get(CASE_PATH.as_posix(), ()) if case["id"] in listed]
    prefix = frozen[:per_court * len(REQUIRED_COURTS)]
    if (
        label != FROZEN_LABEL or len(prefix) != len(drawn)
        or {(*_cell(case), case["opinion_id"]) for case in prefix}
        != {(sample.court, sample.text_source, sample.decade, sample.opinion_id) for sample in drawn}
    ):
        raise DrawError(
            "replay", "stage does not reproduce the frozen draw",
            f"Use stage {FROZEN_LABEL} and seed 20261008 as recorded in the build-gates README, then retry.",
        )
    try:
        from gideon.worker.opiniontext import OpinionTextFailure, canonical_text, choose
    except ImportError as error:
        raise DrawError(
            "check", "ingest check cannot be loaded",
            "Use an interpreter with the pinned worker dependencies installed, then retry.",
        ) from error
    used = {case.get("opinion_id") for case in loaded.cases_by_id.values()}
    candidates = {
        cell: tuple(opinion_id for opinion_id in ids[allocation.get(cell, 0):] if opinion_id not in used)
        for cell, ids in orders.items()
    }
    outcomes: dict[int, bool] = {}
    visited = dict.fromkeys(candidates, 0)
    checked = dict.fromkeys(REQUIRED_COURTS, 0)
    while True:
        selected: list[Successor] = []
        taken: set[int] = set()
        needed: set[Cell] = set()
        for case_id, case in retired.items():
            original = _cell(case)
            alternatives = sorted(
                (cell for cell in candidates if cell[:2] == original[:2] and cell != original),
                key=lambda cell: (abs(cell[2] - original[2]), cell[2]),
            )
            for cell in (original, *alternatives):
                failed = 0
                for rank, opinion_id in enumerate(candidates.get(cell, ()), 1):
                    if opinion_id in taken:
                        continue
                    if opinion_id not in outcomes:
                        needed.add(cell)
                        break
                    if outcomes[opinion_id]:
                        selected.append(Successor(
                            case_id, Sample(opinion_id, cell[0], cell[2], cell[1]),
                            None if cell == original else cell, rank, failed,
                        ))
                        taken.add(opinion_id)
                        break
                    failed += 1
                else:
                    continue
                break
            else:
                raise DrawError(
                    "check", f"no passing successor for {case_id}",
                    "Restore eligible staged opinions for this court and source, then retry.",
                )
        if not needed:
            return tuple(selected), checked
        windows = {cell: candidates[cell][visited[cell]:visited[cell] + CHECK_WINDOW] for cell in sorted(needed)}
        for court in REQUIRED_COURTS:
            window_ids = {opinion_id for cell, ids in windows.items() if cell[0] == court for opinion_id in ids}
            if not window_ids:
                continue
            found: set[int] = set()
            for row in _read_csv(stage_root / court / "opinions.csv", ("id", *SOURCE_COLUMNS), stage="check"):
                try:
                    opinion_id = int(row["id"])
                except ValueError as error:
                    raise DrawError("check", "candidate id is invalid", "Restore the staged opinions, then retry.") from error
                if opinion_id not in window_ids:
                    continue
                choice = choose(row)
                if choice is None or opinion_id in found:
                    raise DrawError("check", "candidate source is missing or repeated", "Restore the staged opinions, then retry.")
                found.add(opinion_id)
                checked[court] += 1
                try:
                    canonical_text(*choice)
                except OpinionTextFailure:
                    outcomes[opinion_id] = False
                else:
                    outcomes[opinion_id] = True
            if found != window_ids:
                raise DrawError("check", "candidate rows are missing", "Restore the staged opinions, then retry.")
        for cell, ids in windows.items():
            visited[cell] += len(ids)


def draw(stage_root: Path, *, seed: int, out: Path, per_court: int, root: Path, stdout: TextIO,
         retire: Sequence[str] = (), set_root: Path | None = None) -> None:
    """Read the staged population and write a content-free sample outside the checkout."""

    if per_court <= 0:
        raise DrawError("allocate", "per-court count must be positive", "Provide a positive --per-court count.")
    output_path = out.resolve()
    checkout = root.resolve()
    if output_path == checkout or checkout in output_path.parents:
        raise DrawError("write", "sample output is inside the checkout", "Choose --out outside the checkout, then retry.")
    if out.exists():
        raise DrawError("write", "sample output already exists", "Choose a new --out path, then retry.")
    label, snapshot, counts = _stage(stage_root)
    cells, skipped, no_cluster = _population(stage_root, counts)
    allocation = allocate(cells, per_court)
    orders = _shuffled(cells, allocation, seed)
    drawn = _draw(orders, allocation)
    if retire:
        loaded = _load_set(root / evalset.SET_ROOT if set_root is None else set_root)
        retired = _retired_cases(loaded, retire)
        successors, checked = _successors(
            stage_root, orders=orders, allocation=allocation, drawn=drawn, label=label,
            per_court=per_court, loaded=loaded, retired=retired,
        )
        _write_successor_sample(
            out, seed=seed, label=label, snapshot=snapshot, per_court=per_court,
            successors=successors, checked=checked,
            retired=retired, stdout=stdout,
        )
        return
    population_rows = [
        {"court": court, "text_source": source, "decade": decade, "count": len(ids)}
        for (court, source, decade), ids in sorted(cells.items())
    ]
    allocation_rows = [
        {"court": court, "text_source": source, "decade": decade, "count": count}
        for (court, source, decade), count in sorted(allocation.items())
    ]
    header = {"seed": seed, "label": label, "snapshot": snapshot, "per_court": per_court,
              "population": population_rows, "allocation": allocation_rows, "skipped_no_text": skipped,
              "skipped_no_cluster": no_cluster}
    payload = _json_line(header) + "".join(_json_line({
        "opinion_id": sample.opinion_id, "court": sample.court,
        "decade": sample.decade, "text_source": sample.text_source,
    }) for sample in drawn)
    try:
        # Run under sudo, the sample goes to whoever owns where it is written,
        # the person who runs the --write form next.
        owner = manifest.owner_of(out.parent)
        manifest.make_directory(out.parent, owner)
        manifest.write_atomic(out, payload.encode("utf-8"), owner=owner)
    except OSError as error:
        raise DrawError("write", "sample cannot be written", "Choose a writable --out path, then retry.") from error
    for row in population_rows:
        print(f"population {row['court']} {row['text_source']} {row['decade']}: {row['count']}", file=stdout)
    for row in allocation_rows:
        print(f"allocation {row['court']} {row['text_source']} {row['decade']}: {row['count']}", file=stdout)
    print(
        f"drawn: {len(drawn)}; no text: {sum(skipped.values())}; "
        f"no cluster: {sum(no_cluster.values())}",
        file=stdout,
    )


def _write_successor_sample(
    out: Path, *, seed: int, label: str, snapshot: str, per_court: int,
    successors: Sequence[Successor], checked: Mapping[str, int],
    retired: Mapping[str, evalset.Case], stdout: TextIO,
) -> None:
    header = {
        "seed": seed, "label": label, "snapshot": snapshot, "per_court": per_court,
        "mode": "successors", "retired": [
            {"case_id": item.retired, **_cell_row(_cell(retired[item.retired])),
             "fallback": None if item.fallback is None else _cell_row(item.fallback)}
            for item in successors
        ], "checked": checked,
    }
    payload = _json_line(header) + "".join(_json_line({
        "opinion_id": item.sample.opinion_id, "court": item.sample.court,
        "decade": item.sample.decade, "text_source": item.sample.text_source,
        "supersedes": item.retired,
    }) for item in successors)
    try:
        owner = manifest.owner_of(out.parent)
        manifest.make_directory(out.parent, owner)
        manifest.write_atomic(out, payload.encode("utf-8"), owner=owner)
    except OSError as error:
        raise DrawError("write", "sample cannot be written", "Choose a writable --out path, then retry.") from error
    for item in successors:
        court, source, decade = _cell(retired[item.retired])
        fallback = "" if item.fallback is None else f"; fallback: {item.fallback[0]} {item.fallback[1]} {item.fallback[2]}"
        print(
            f"{item.retired}: {court} {source} {decade}{fallback}; opinion: {item.sample.opinion_id}; "
            f"rank: {item.rank}; checked and failed: {item.checked}", file=stdout,
        )
    print(f"successors: {len(successors)}; checked: {sum(checked.values())}", file=stdout)


def _case_line(case_id: str, sample: Sample, reviewer: str, on: str, supersedes: str | None = None) -> str:
    record: dict[str, object] = {
        "id": case_id, "suite": "build-gates", "category": "segmenter-300", "branch": "legal",
        "opinion_id": sample.opinion_id, "court": sample.court, "decade": sample.decade,
        "text_source": sample.text_source, "labels": ["drawn"], "cluster_id": case_id,
        "review": {"by": reviewer, "on": on}, "notes": "",
    }
    if supersedes is not None:
        record["supersedes"] = supersedes
    return _json_line(record)


def _validated_cell(item: Mapping[str, object]) -> Cell:
    court, source, decade = item.get("court"), item.get("text_source"), item.get("decade")
    if (
        court not in REQUIRED_COURTS or source not in SOURCE_COLUMNS
        or type(decade) is not int or decade % 10
    ):
        raise DrawError("write", "successor cell is invalid", "Re-run the successor draw, then retry.")
    return cast(str, court), cast(str, source), decade


def _write_successors(
    header: Mapping[str, object], rows: Sequence[object], *, reviewer: str, on: str,
    set_root: Path, stdout: TextIO,
) -> None:
    """Validate replacements and append all three files, restoring replaced bytes on failure."""

    fix = "Re-run the successor draw against this set, then retry."
    per_court, retired_rows, checked = header.get("per_court"), header.get("retired"), header.get("checked")
    if (
        tuple(header) != ("seed", "label", "snapshot", "per_court", "mode", "retired", "checked")
        or type(header.get("seed")) is not int
        or not isinstance(header.get("label"), str) or not isinstance(header.get("snapshot"), str)
        or type(per_court) is not int or per_court <= 0
        or not isinstance(retired_rows, list) or len(retired_rows) != len(rows)
        or not isinstance(checked, dict) or set(checked) != set(REQUIRED_COURTS)
        or any(type(count) is not int or count < 0 for count in checked.values())
    ):
        raise DrawError("write", "successor header is invalid", fix)
    samples: list[Sample] = []
    requested: list[str] = []
    for item in rows:
        if not isinstance(item, dict) or tuple(item) != ("opinion_id", "court", "decade", "text_source", "supersedes"):
            raise DrawError("write", "successor row is invalid", fix)
        opinion_id, supersedes = item["opinion_id"], item["supersedes"]
        if (
            type(opinion_id) is not int or opinion_id <= 0 or not isinstance(supersedes, str)
            or evalset.SEGMENTER_ID_PATTERN.fullmatch(supersedes) is None
        ):
            raise DrawError("write", "successor opinion or retired id is invalid", fix)
        court, source, decade = _validated_cell(item)
        samples.append(Sample(opinion_id, court, decade, source))
        requested.append(supersedes)
    if requested != sorted(requested) or len({sample.opinion_id for sample in samples}) != len(samples):
        raise DrawError("write", "successor rows are repeated or unordered", fix)
    paths = (set_root / CASE_PATH, set_root / IDS_PATH, set_root / MIRROR_IDS_PATH)
    try:
        previous = {path: path.read_bytes() for path in paths}
    except OSError as error:
        raise DrawError("write", "successor files cannot be read", "Restore all three frozen sample files, then retry.") from error
    if any(not raw.endswith(b"\n") for raw in previous.values()):
        raise DrawError("write", "successor files lack a final newline", "Restore newline-terminated frozen sample files, then retry.")
    if previous[paths[1]] != previous[paths[2]]:
        raise DrawError("write", "slice and mirror id lists differ", "Restore the byte-equal slice and mirror lists, then retry.")
    loaded = _load_set(set_root)
    retired = _retired_cases(loaded, requested)
    used = {case.get("opinion_id") for case in loaded.cases_by_id.values()}
    for case_id, sample, metadata in zip(requested, samples, retired_rows, strict=True):
        if (
            not isinstance(metadata, dict)
            or tuple(metadata) != ("case_id", "court", "text_source", "decade", "fallback")
            or metadata["case_id"] != case_id
        ):
            raise DrawError("write", "retirement record is invalid", fix)
        original = _cell(retired[case_id])
        if _validated_cell(metadata) != original:
            raise DrawError("write", "retired cell differs from the set", fix)
        fallback = metadata["fallback"]
        expected = original
        if fallback is not None:
            if not isinstance(fallback, dict) or tuple(fallback) != ("court", "text_source", "decade"):
                raise DrawError("write", "fallback record is invalid", fix)
            expected = _validated_cell(fallback)
            if expected[:2] != original[:2] or expected == original:
                raise DrawError("write", "fallback court, source, or decade is invalid", fix)
        if (sample.court, sample.text_source, sample.decade) != expected or sample.opinion_id in used:
            raise DrawError("write", "successor cell differs or opinion is already a case", fix)
    highest = max(
        int(cast(str, case["id"]).removeprefix("segmenter-"))
        for case in loaded.cases_by_file[CASE_PATH.as_posix()]
        if evalset.SEGMENTER_ID_PATTERN.fullmatch(cast(str, case["id"]))
    )
    ids = [f"segmenter-{highest + index:03d}" for index in range(1, len(samples) + 1)]
    case_bytes = "".join(
        _case_line(case_id, sample, reviewer, on, supersedes)
        for case_id, sample, supersedes in zip(ids, samples, requested, strict=True)
    ).encode("utf-8")
    ids_bytes = ("\n".join(ids) + "\n").encode("utf-8")
    payloads = {
        paths[0]: previous[paths[0]] + case_bytes,
        paths[1]: previous[paths[1]] + ids_bytes,
        paths[2]: previous[paths[2]] + ids_bytes,
    }
    replaced: list[Path] = []
    try:
        for path, payload in payloads.items():
            # Include this attempt in the restore: an OSError can follow the
            # replacement itself during write_atomic's final cleanup.
            replaced.append(path)
            manifest.write_atomic(path, payload)
    except OSError as error:
        restore_failed = False
        for path in reversed(replaced):
            try:
                manifest.write_atomic(path, previous[path])
            except OSError:
                restore_failed = True
        problem = "successor files cannot be restored" if restore_failed else "successor files cannot be written"
        raise DrawError("write", problem, "Restore all three frozen sample files to their previous bytes, then retry.") from error
    for name, payload in zip(("cases", "ids", "mirror ids"), payloads.values(), strict=True):
        print(f"{name}: {len(payload.splitlines())} lines sha256 {hashlib.sha256(payload).hexdigest()}", file=stdout)


def write(sample_path: Path, *, reviewer: str, on: str, set_root: Path, stdout: TextIO) -> None:
    """Write a plain sample or append successor cases and their mirrored id lists."""

    if evalset.ROLE_PATTERN.fullmatch(reviewer) is None or not evalset.valid_iso_date(on):
        raise DrawError("write", "review stamp is invalid", "Provide a role id and ISO date, then retry.")
    try:
        lines = sample_path.read_text(encoding="utf-8").splitlines()
        documents = [json.loads(line) for line in lines]
    except (OSError, UnicodeError, ValueError) as error:
        raise DrawError("write", "sample cannot be read", "Provide a drawn sample file, then retry.") from error
    if not documents or not isinstance(documents[0], dict) or len(documents) < 2:
        raise DrawError("write", "sample is incomplete", "Provide a drawn sample file, then retry.")
    header = documents[0]
    if header.get("mode") == "successors":
        _write_successors(header, documents[1:], reviewer=reviewer, on=on, set_root=set_root, stdout=stdout)
        return
    per_court = header.get("per_court")
    if type(per_court) is not int or per_court <= 0 or set(header.get("skipped_no_text", {})) != set(REQUIRED_COURTS):
        raise DrawError("write", "sample header is invalid", "Re-run the draw, then retry.")
    samples: list[Sample] = []
    for item in documents[1:]:
        if not isinstance(item, dict) or tuple(item) != ("opinion_id", "court", "decade", "text_source"):
            raise DrawError("write", "sample row is invalid", "Re-run the draw, then retry.")
        opinion_id, court, decade, source = (item[key] for key in ("opinion_id", "court", "decade", "text_source"))
        if (type(opinion_id) is not int or opinion_id <= 0 or court not in REQUIRED_COURTS
            or type(decade) is not int or decade % 10 or source not in SOURCE_COLUMNS):
            raise DrawError("write", "sample row is invalid", "Re-run the draw, then retry.")
        samples.append(Sample(opinion_id, court, decade, source))
    if (
        len(samples) != per_court * len(REQUIRED_COURTS)
        or len({sample.opinion_id for sample in samples}) != len(samples)
        or samples != sorted(samples, key=lambda sample: (sample.court, sample.text_source, sample.decade, sample.opinion_id))
    ):
        raise DrawError("write", "sample rows are incomplete or unordered", "Re-run the draw, then retry.")
    case_path = set_root / CASE_PATH
    ids_path = set_root / IDS_PATH
    if any(path.exists() and path.stat().st_size for path in (case_path, ids_path)):
        raise DrawError("write", "segmenter cases or id list already has lines", "Keep the frozen sample; correct it by appending a successor case.")
    cases: list[str] = []
    ids: list[str] = []
    for ordinal, sample in enumerate(samples, 1):
        case_id = f"segmenter-{ordinal:03d}"
        ids.append(case_id)
        cases.append(_case_line(case_id, sample, reviewer, on))
    case_bytes = "".join(cases).encode("utf-8")
    ids_bytes = ("\n".join(ids) + "\n").encode("utf-8")
    case_existed = case_path.exists()
    try:
        manifest.write_atomic(case_path, case_bytes)
        manifest.write_atomic(ids_path, ids_bytes)
    except OSError as error:
        try:
            if case_existed:
                manifest.write_atomic(case_path, b"")
            else:
                case_path.unlink(missing_ok=True)
        except OSError:
            raise DrawError("write", "sample files cannot be restored", "Restore both sample files, then retry.") from error
        raise DrawError("write", "cases or id list cannot be written", "Restore both sample files, then retry.") from error
    print(f"cases: {len(cases)} lines sha256 {hashlib.sha256(case_bytes).hexdigest()}", file=stdout)
    print(f"ids: {len(ids)} lines sha256 {hashlib.sha256(ids_bytes).hexdigest()}", file=stdout)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draw or write a frozen segmenter sample")
    parser.add_argument("stage_root", nargs="?", type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--per-court", type=int, default=150)
    parser.add_argument("--write", type=Path)
    parser.add_argument("--reviewer")
    parser.add_argument("--on")
    parser.add_argument("--set", dest="set_root", type=Path)
    parser.add_argument("--retire", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None, *, root: Path | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Run one draw form and return its command status."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    checkout = DEFAULT_ROOT if root is None else root
    try:
        if args.write is None:
            if (args.stage_root is None or args.seed is None or args.out is None
                or args.reviewer is not None or args.on is not None
                or (args.set_root is not None and not args.retire)):
                raise DrawError("draw", "draw arguments are incomplete", "Provide a stage root, --seed, and --out; use --set only with --retire.")
            draw(args.stage_root, seed=args.seed, out=args.out, per_court=args.per_court,
                 root=checkout, stdout=output, retire=args.retire, set_root=args.set_root)
        else:
            if (args.stage_root is not None or args.seed is not None or args.out is not None
                or args.reviewer is None or args.on is None or args.retire):
                raise DrawError("write", "write arguments are incomplete", "Provide --write, --reviewer, and --on; use --retire only on the draw side.")
            write(args.write, reviewer=args.reviewer, on=args.on,
                  set_root=args.set_root if args.set_root is not None else checkout / evalset.SET_ROOT,
                  stdout=output)
    except DrawError as error:
        print(f"{error.stage}: refused — {error.problem}. Fix: {error.fix}", file=errors)
        return 1
    except Exception as error:  # noqa: BLE001 - command boundary keeps staged text out of diagnostics
        print(f"segmenter draw: internal error ({type(error).__name__}). Fix: Repair the draw inputs or tool, then retry.", file=errors)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
