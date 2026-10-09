"""Fetch the pinned sentences and print the shipped pattern set's figures."""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from http.client import HTTPException
from pathlib import Path
from typing import Any, Final, Never, TextIO
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from gideon.host.egress import load_egress_allowlist
from tools.treatment import DATASET, Figures, PinnedFile, measure, read, resolve_url
from tools.treatment.arms import ARMS, Arm, arm, compose, shipped

_CHUNK: Final = 64 * 1024
_MAX_REDIRECTS: Final = 5
_FETCH_COMMAND: Final = "python3 -m tools.treatment fetch --to"


class _UsageError(Exception):
    pass


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise _UsageError(message)

    def exit(self, status: int = 0, message: str | None = None) -> Never:
        if message:
            print(message, end="")
        raise _ParserExit(status)


def _parser() -> _Parser:
    parser = _Parser(prog="python3 -m tools.treatment")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("--to", type=Path, required=True, metavar="DIR")
    measured = commands.add_parser("measure")
    measured.add_argument("--data", type=Path, required=True, metavar="DIR")
    measured.add_argument("--arm", action="append", metavar="NAME")
    measured.add_argument("--compose", metavar="NAMES")
    measured.add_argument("--ids", action="store_true")
    return parser


def _refusal(problem: str, fix: str, stream: TextIO) -> int:
    print(f"tools.treatment: {problem} Fix: {fix}", file=stream)
    return 1


def _verified(path: Path, pinned: PinnedFile) -> bool:
    if not path.is_file() or path.stat().st_size != pinned.size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest() == pinned.sha256


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, request: Request, fp: Any, code: int, message: str,
        headers: Any, newurl: str,
    ) -> None:
        return None


def _open(request: Request) -> Any:
    try:
        return build_opener(_NoRedirect()).open(request, timeout=30)
    except HTTPError as exc:
        return exc


def _download(
    pinned: PinnedFile,
    partial: Path,
    allowed: set[str],
    opener: Callable[[Request], Any],
) -> str | None:
    url = resolve_url(pinned)
    traversed: list[str] = []
    for redirect in range(_MAX_REDIRECTS + 1):
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme != "https" or host is None:
            return f"refused non-HTTPS dataset URL after {len(traversed)} hop(s)"
        traversed.append(host)
        if host not in allowed:
            return f"host {host} is outside the install-upgrade egress group"
        try:
            response = opener(Request(url, method="GET"))
        except HTTPError as exc:
            response = exc
        try:
            status = response.status
            if status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    return f"redirect from {host} has no Location header"
                if redirect == _MAX_REDIRECTS:
                    return f"too many redirects for {pinned.path}"
                url = urljoin(url, location)
                continue
            if status != 200:
                return f"{pinned.path} returned HTTP {status} from {host}"
            digest = hashlib.sha256()
            size = 0
            with partial.open("wb") as stream:
                while chunk := response.read(_CHUNK):
                    size += len(chunk)
                    if size > pinned.size:
                        return f"{pinned.path} size or sha256 differs from its pin"
                    digest.update(chunk)
                    stream.write(chunk)
            if size != pinned.size or digest.hexdigest() != pinned.sha256:
                return f"{pinned.path} size or sha256 differs from its pin"
            return None
        finally:
            response.close()
    return f"too many redirects for {pinned.path}"


def _fetch(
    destination: Path, root: Path, opener: Callable[[Request], Any],
    output: TextIO, error: TextIO,
) -> int:
    loaded = load_egress_allowlist(root / "config/egress.yaml")
    if loaded.errors or loaded.allowlist is None:
        problem = loaded.errors[0].problem if loaded.errors else "egress allowlist is unavailable"
        return _refusal(problem, "Correct config/egress.yaml, then re-run fetch.", error)
    group = loaded.allowlist.group("install-upgrade")
    if group is None:
        return _refusal(
            "install-upgrade egress group is missing",
            "Correct config/egress.yaml, then re-run fetch.", error,
        )
    allowed = {entry.host for entry in group.hosts}
    print(
        f"Licence: {DATASET.licence}; LegalBench states it for this task; "
        f"the original authors publish no licence line. Attribution: {DATASET.attribution}",
        file=output,
    )
    for pinned in DATASET.files:
        path = destination / pinned.path
        partial = path.with_name(path.name + ".partial")
        try:
            try:
                if _verified(path, pinned):
                    print(f"{pinned.path}: verified", file=output)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                download_problem = _download(pinned, partial, allowed, opener)
                if download_problem is not None:
                    return _refusal(
                        download_problem,
                        "Check the pin and host against config/egress.yaml's "
                        "install-upgrade group, then re-run fetch.",
                        error,
                    )
                partial.replace(path)
                print(f"{pinned.path}: fetched and verified", file=output)
            finally:
                partial.unlink(missing_ok=True)
        except (OSError, HTTPException, ValueError) as exc:
            return _refusal(
                f"{pinned.path} could not be fetched or checked ({type(exc).__name__})",
                "Check the file access and the host, then re-run fetch.", error,
            )
    return 0


def _figure_line(selected: Arm, unit: str, figures: Figures) -> str:
    diagnostic = " diagnostic" if not selected.selectable else ""
    return (
        f"{selected.name} {unit} {figures.predicted} {figures.true_positives} "
        f"{figures.false_positives} {figures.false_negatives} "
        f"{figures.precision:.3f} {figures.recall:.3f}{diagnostic}"
    )


def _measure(
    destination: Path, selected: Sequence[Arm], ids: bool,
    output: TextIO, error: TextIO,
) -> int:
    fix = f"Run {_FETCH_COMMAND} {destination}, then re-run measure."
    for pinned in DATASET.files:
        try:
            valid = _verified(destination / pinned.path, pinned)
        except OSError:
            valid = False
        if not valid:
            return _refusal(f"{pinned.path} is missing or differs from its pin", fix, error)
    try:
        sentences = read(destination)
        results = tuple((choice, measure(sentences, choice.rules)) for choice in selected)
    except (OSError, UnicodeError, ValueError) as exc:
        return _refusal(
            f"dataset could not be read ({type(exc).__name__}: {exc})",
            f"Check the pinned files; {_FETCH_COMMAND} {destination} restores them.",
            error,
        )
    print("arm unit predicted tp fp fn precision recall", file=output)
    for choice, result in results:
        for unit, figures in (("edge", result.edge), ("sentence", result.sentence)):
            print(_figure_line(choice, unit, figures), file=output)
    first_result = results[0][1]
    coverage = first_result.anchored_positive + first_result.anchored_negative
    print(
        f"anchored coverage: {coverage} (positive {first_result.anchored_positive}, "
        f"negative {first_result.anchored_negative})", file=output,
    )
    for choice, result in results:
        print(f"{choice.name} lineage-only: {result.lineage_only}", file=output)
        verbs = " ".join(f"{name}={count}" for name, count in result.per_verb) or "none"
        print(f"{choice.name} per verb: {verbs}", file=output)
    if ids:
        for choice, result in results:
            for unit, figures in (("edge", result.edge), ("sentence", result.sentence)):
                print(
                    f"{choice.name} {unit} fp ids: {', '.join(figures.false_positive_ids)}",
                    file=output,
                )
                print(
                    f"{choice.name} {unit} fn ids: {', '.join(figures.false_negative_ids)}",
                    file=output,
                )
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    opener: Callable[[Request], Any] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    root: Path | None = None,
) -> int:
    """Run the baseline fetch or measure command with injectable I/O."""

    output = stdout or sys.stdout
    error = stderr or sys.stderr
    parser = _parser()
    try:
        with redirect_stdout(output):
            options = parser.parse_args(argv)
    except _UsageError as exc:
        parser.print_usage(file=error)
        print(f"{parser.prog}: error: {exc}", file=error)
        return 2
    except _ParserExit as exc:
        return exc.status
    if options.command == "fetch":
        checkout = root or Path(__file__).resolve().parents[2]
        return _fetch(options.to, checkout, opener or _open, output, error)
    try:
        if options.arm:
            selected = tuple(arm(name) for name in options.arm)
        else:
            current = shipped()
            selected = ARMS if current in ARMS else (*ARMS, current)
        if options.compose is not None:
            names = tuple(name.strip() for name in options.compose.split(","))
            selected += (compose(names),)
    except ValueError as exc:
        print(f"{parser.prog}: error: {exc}", file=error)
        return 2
    return _measure(options.data, selected, options.ids, output, error)


if __name__ == "__main__":
    raise SystemExit(main())
