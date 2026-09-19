"""Run the local-only court-map generator as ``python3 -m tools.courtmap``."""

import argparse
import bz2
import csv
import hashlib
import io
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

from gideon.host.courts import SOURCE_FILE, CourtMap, load_court_map_text
from gideon.host.report import refusal
from tools.courtmap import generate, geography

_CSV_COLUMNS: Final = ("id", "jurisdiction", "full_name")
_OUTPUT_NAME: Final = "courts.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tools.courtmap")
    parser.add_argument(
        "--csv",
        required=True,
        type=Path,
        metavar="PATH",
        help="local courts-YYYY-MM-DD.csv.bz2 source; never fetched by this tool",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the generated text with courts.yaml without writing",
    )
    return parser


def _refusal(problem: object, fix: str) -> str:
    return refusal("tools.courtmap", problem, fix)


def _read_csv(path: Path) -> tuple[str, str, list[dict[str, object]]] | str:
    try:
        compressed = path.read_bytes()
    except OSError as exc:
        return _refusal(
            f"CSV source is unreadable: {path} ({exc})",
            "Provide the local CSV named by --csv; this tool never fetches it.",
        )
    match = SOURCE_FILE.fullmatch(path.name)
    if match is None:
        return _refusal(
            f"CSV source name is not courts-YYYY-MM-DD.csv.bz2: {path.name}",
            "Rename the local source to courts-YYYY-MM-DD.csv.bz2, then re-run.",
        )
    try:
        text = bz2.decompress(compressed).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _refusal(
            f"CSV source is not valid bzip2 UTF-8 data: {path} ({exc})",
            "Provide the verified local CSV source; this tool never fetches it.",
        )
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fieldnames = reader.fieldnames
        if fieldnames is None or any(column not in fieldnames for column in _CSV_COLUMNS):
            return _refusal(
                f"CSV source is missing one of the required columns: {_CSV_COLUMNS}",
                "Provide a CourtListener courts CSV with id, jurisdiction, and full_name columns.",
            )
        rows = [cast(dict[str, object], row) for row in reader]
    except csv.Error as exc:
        return _refusal(
            f"CSV source has a parse error: {exc}",
            "Provide a standard CourtListener courts CSV, then re-run.",
        )
    return match.group(1), hashlib.sha256(compressed).hexdigest(), rows


def _round_trip(court_map: CourtMap, text: str) -> str | None:
    loaded = load_court_map_text(text)
    if loaded.errors or loaded.court_map is None:
        return (
            "the generated courts.yaml text did not load through gideon.host.courts: "
            + "; ".join(error.problem for error in loaded.errors)
        )
    if loaded.court_map != court_map:
        return "the generated courts.yaml text changed the loaded court map"
    return None


def _round_trip_fix() -> str:
    return "Correct tools/courtmap/generate.py, then re-run python3 -m tools.courtmap."


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
) -> int:
    """Generate or check the committed court map from a local compressed CSV."""

    options = _parser().parse_args(argv)
    checkout = root or Path(__file__).resolve().parents[2]
    source = _read_csv(options.csv)
    if isinstance(source, str):
        print(source, file=sys.stderr)
        return 1
    source_date, source_hash, rows = source

    geography_result = geography.load_geography(checkout / "tools/courtmap/geography.yaml")
    if geography_result.errors or geography_result.geography is None:
        print(geography.render_errors(geography_result.errors), file=sys.stderr)
        return 1
    generated = generate.generate(
        rows,
        geography_result.geography,
        file=options.csv.name,
        date=source_date,
        sha256=source_hash,
    )
    if not isinstance(generated, CourtMap):
        print(generate.render_errors(generated), file=sys.stderr)
        return 1
    text = generate.render_court_map(generated)
    round_trip_problem = _round_trip(generated, text)
    if round_trip_problem is not None:
        print(_refusal(round_trip_problem, _round_trip_fix()), file=sys.stderr)
        return 1

    output = checkout / _OUTPUT_NAME
    if options.check:
        try:
            actual = output.read_text(encoding="utf-8")
        except OSError as exc:
            print(
                _refusal(
                    f"{_OUTPUT_NAME} is unreadable: {output} ({exc})",
                    "Run python3 -m tools.courtmap --csv <local courts-YYYY-MM-DD.csv.bz2> to write it.",
                ),
                file=sys.stderr,
            )
            return 1
        if actual != text:
            print(
                _refusal(
                    f"{_OUTPUT_NAME} differs from the generated court map",
                    "Run python3 -m tools.courtmap --csv <local courts-YYYY-MM-DD.csv.bz2>.",
                ),
                file=sys.stderr,
            )
            return 1
        print(f"{_OUTPUT_NAME}: check passed")
        return 0

    try:
        output.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(
            _refusal(
                f"cannot write {output}: {exc}",
                "Make the checkout writable, then re-run python3 -m tools.courtmap.",
            ),
            file=sys.stderr,
        )
        return 1
    print(f"{_OUTPUT_NAME}: generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
