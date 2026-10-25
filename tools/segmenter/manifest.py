"""Shared manifest fields, slice-file pins, sheet headers, and atomic writes
for the segmenter kit."""

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

MANIFEST_FORMAT: Final[int] = 2
MANIFEST_KEYS: Final[tuple[str, ...]] = (
    "format", "set_root", "set_digest", "slice_pins", "prompt", "prompt_digest", "cases",
)
CASE_KEYS: Final[tuple[str, ...]] = (
    "case_id", "opinion_id", "doc_id", "sha256", "length", "starts",
    "status", "failure_code", "sheet_sha256",
)
SHEET_PARAGRAPHS_HEADER: Final[str] = "Paragraphs:"
CASE_PATH: Final[Path] = Path("build-gates/segmenter-300.jsonl")
IDS_PATH: Final[Path] = Path("slices/segmenter-300/sample.ids")
MIRROR_IDS_PATH: Final[Path] = Path("slices/build-gates/sample.ids")


def pin_slice(set_root: Path) -> dict[str, tuple[int, str]]:
    """Record the line count and byte-prefix digest of each slice file."""

    pins: dict[str, tuple[int, str]] = {}
    for path in (CASE_PATH, IDS_PATH):
        raw = (set_root / path).read_bytes()
        pins[path.as_posix()] = (len(raw.splitlines()), hashlib.sha256(raw).hexdigest())
    return pins


def hold_slice(set_root: Path, pins: Mapping[str, tuple[int, str]]) -> bool:
    """Hold recorded byte prefixes while allowing newline-terminated appends."""

    for path in (CASE_PATH, IDS_PATH):
        count, digest = pins[path.as_posix()]
        raw = (set_root / path).read_bytes()
        lines = raw.splitlines(keepends=True)
        if (
            not raw.endswith(b"\n") or len(lines) < count
            or hashlib.sha256(b"".join(lines[:count])).hexdigest() != digest
        ):
            return False
    return True


def sheet_header(case_id: str, opinion_id: int, court: str, sha256: str,
                 length: int, paragraphs: int, status: str) -> str:
    """The content-free first line of a review sheet."""

    return (
        f"case {case_id} | opinion {opinion_id} | court {court} | "
        f"sha256 {sha256[:12]} | length {length} | paragraphs {paragraphs} | status {status}"
    )


def owner_of(path: Path) -> tuple[int, int]:
    """The uid and gid of *path*'s nearest existing directory, which files
    written beneath it under sudo are handed to."""

    anchor = next(item for item in (path, *path.parents) if item.exists())
    status = anchor.stat()
    return status.st_uid, status.st_gid


def make_directory(path: Path, owner: tuple[int, int]) -> None:
    """Create *path* and any missing parents, each handed to *owner*."""

    missing = [item for item in (path, *path.parents) if not item.exists()]
    for item in reversed(missing):
        item.mkdir()
        os.chown(item, *owner)


def write_atomic(path: Path, payload: bytes, *, owner: tuple[int, int] | None = None) -> None:
    """Replace *path* with *payload* whole, or leave it as it was.

    The file keeps the mode of the one it replaces, or takes 0644, rather than
    the temporary file's 0600; under sudo, *owner* hands it back to the person
    who reads and edits it next.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fchmod(output.fileno(), mode)
            if owner is not None:
                os.fchown(output.fileno(), *owner)
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
