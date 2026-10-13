"""What the segmenter kit's commands share: the manifest's field order, the
sheet's header, and the atomic file write."""

import os
import tempfile
from pathlib import Path
from typing import Final

MANIFEST_FORMAT: Final[int] = 1
MANIFEST_KEYS: Final[tuple[str, ...]] = (
    "format", "set_root", "set_digest", "prompt", "prompt_digest", "cases",
)
CASE_KEYS: Final[tuple[str, ...]] = (
    "case_id", "opinion_id", "doc_id", "sha256", "length", "starts",
    "status", "failure_code", "sheet_sha256",
)
SHEET_PARAGRAPHS_HEADER: Final[str] = "Paragraphs:"


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
