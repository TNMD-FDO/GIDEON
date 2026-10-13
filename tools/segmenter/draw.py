"""Draw a frozen segmenter sample from staged court opinions."""

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
from typing import Final, TextIO

from gideon.evaluation import evalset
from tools.segmenter import manifest

DEFAULT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
REQUIRED_COURTS: Final[tuple[str, ...]] = ("ca6", "scotus")
CASE_PATH: Final[Path] = Path("build-gates/segmenter-300.jsonl")
IDS_PATH: Final[Path] = Path("slices/segmenter-300/sample.ids")
SOURCE_COLUMNS: Final[tuple[str, ...]] = evalset.SEGMENTER_TEXT_SOURCES
CSV_OPTIONS: Final[Mapping[str, object]] = {
    "delimiter": ",", "quotechar": '"', "escapechar": "\\",
    "doublequote": False, "strict": True,
}
# The staged dump has opinion text fields measured in megabytes.
CSV_FIELD_LIMIT: Final[int] = sys.maxsize
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


def _draw(cells: Mapping[Cell, Sequence[int]], allocation: Mapping[Cell, int], seed: int) -> tuple[Sample, ...]:
    randomizer = random.Random(seed)
    drawn: list[Sample] = []
    for court, source, decade in sorted(allocation):
        ids = sorted(cells[(court, source, decade)])
        randomizer.shuffle(ids)
        drawn.extend(Sample(opinion_id, court, decade, source) for opinion_id in ids[:allocation[(court, source, decade)]])
    return tuple(sorted(drawn, key=lambda sample: (sample.court, sample.text_source, sample.decade, sample.opinion_id)))


def _json_line(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def draw(stage_root: Path, *, seed: int, out: Path, per_court: int, root: Path, stdout: TextIO) -> None:
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
    drawn = _draw(cells, allocation, seed)
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


def write(sample_path: Path, *, reviewer: str, on: str, set_root: Path, stdout: TextIO) -> None:
    """Append the frozen case lines and their one id list from a drawn sample."""

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
        cases.append(_json_line({
            "id": case_id, "suite": "build-gates", "category": "segmenter-300", "branch": "legal",
            "opinion_id": sample.opinion_id, "court": sample.court, "decade": sample.decade,
            "text_source": sample.text_source, "labels": ["drawn"], "cluster_id": case_id,
            "review": {"by": reviewer, "on": on}, "notes": "",
        }))
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
                or args.reviewer is not None or args.on is not None or args.set_root is not None):
                raise DrawError("draw", "draw arguments are incomplete", "Provide a stage root, --seed, and --out.")
            draw(args.stage_root, seed=args.seed, out=args.out, per_court=args.per_court, root=checkout, stdout=output)
        else:
            if args.stage_root is not None or args.seed is not None or args.out is not None or args.reviewer is None or args.on is None:
                raise DrawError("write", "write arguments are incomplete", "Provide --write, --reviewer, and --on.")
            write(args.write, reviewer=args.reviewer, on=args.on,
                  set_root=args.set_root if args.set_root is not None else checkout / evalset.SET_ROOT,
                  stdout=output)
    except DrawError as error:
        print(f"{error.stage}: refused — {error.problem}. Fix: {error.fix}", file=errors)
        return 1
    except Exception as error:  # noqa: BLE001 - command boundary keeps staged text out of diagnostics
        print(f"segmenter draw: internal error ({type(error).__name__})", file=errors)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
