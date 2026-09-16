"""Hugging Face Hub reads and model-file record rendering for the pin watch."""

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import quote, urljoin

import yaml  # type: ignore[import-untyped]

from gideon.host import images, models, weights
from tools.pinwatch.fetch import (
    Fetcher,
    FetchError,
    Response,
    UrllibFetcher,
    next_link,
)

_FILES_INDENT: Final = 8
_FILE_PATH_INDENT: Final = _FILES_INDENT + 2
_FIX: Final = (
    "Check the repository and revision on the Hugging Face Hub, then retry."
)


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One regular file returned by the Hub's revision tree."""

    path: str
    size: int
    sha256: str | None


def _model_url(repo: str) -> str:
    return f"{weights.HF_RESOLVE_ROOT}/api/models/{quote(repo, safe='/')}"


def _tree_url(repo: str, revision: str) -> str:
    return (
        f"{weights.HF_RESOLVE_ROOT}/api/models/{quote(repo, safe='/')}/tree/"
        f"{quote(revision, safe='')}?recursive=true"
    )


def _file_url(repo: str, revision: str, path: str) -> str:
    return (
        f"{weights.HF_RESOLVE_ROOT}/{quote(repo, safe='/')}/resolve/"
        f"{quote(revision, safe='')}/{quote(path, safe='/')}"
    )


def _http_reason(response: Response) -> str:
    if response.status == 401:
        return (
            "HTTP status 401; the repository may be gated, private, or absent "
            "to an anonymous caller"
        )
    if response.status == 429:
        return "HTTP status 429; the anonymous Hub request limit was reached"
    return f"HTTP status {response.status}"


def _json(url: str, response: Response, description: str) -> object:
    if not 200 <= response.status < 300:
        raise FetchError(url, _http_reason(response))
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(url, f"{description} is not valid JSON: {exc}") from None


def model_head(fetcher: Fetcher, repo: str) -> str:
    """Return the default-branch commit hash recorded by the Hub."""

    url = _model_url(repo)
    document = _json(url, fetcher.get(url), "model response")
    if not isinstance(document, Mapping):
        raise FetchError(url, "model response is not an object")
    revision = document.get("sha")
    if not isinstance(revision, str) or models.REVISION.fullmatch(revision) is None:
        raise FetchError(url, "model response has no valid 40-hex sha")
    return revision


def _leading_dot(path: str) -> bool:
    return any(component.startswith(".") for component in PurePosixPath(path).parts)


def _tree_entry(url: str, value: object) -> TreeEntry | None:
    if not isinstance(value, Mapping):
        raise FetchError(url, "tree entry is not an object")
    if value.get("type") != "file":
        return None
    path = value.get("path")
    if not isinstance(path, str):
        raise FetchError(url, "file entry has no text path")
    if _leading_dot(path):
        return None
    if models.FILE_PATH.fullmatch(path) is None:
        raise FetchError(url, f"file entry has an invalid path {path!r}")
    size = value.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise FetchError(url, f"file entry {path!r} has an invalid size")

    sha256: str | None = None
    if "lfs" in value:
        lfs = value["lfs"]
        oid = lfs.get("oid") if isinstance(lfs, Mapping) else None
        digest = f"sha256:{oid}" if isinstance(oid, str) else ""
        if images.DIGEST.fullmatch(digest) is None:
            raise FetchError(url, f"file entry {path!r} has an invalid lfs.oid")
        sha256 = digest
    return TreeEntry(path, size, sha256)


def list_tree(fetcher: Fetcher, repo: str, revision: str) -> tuple[TreeEntry, ...]:
    """List regular files at *revision*, following every Hub tree page."""

    url = _tree_url(repo, revision)
    entries: list[TreeEntry] = []
    seen_paths: set[str] = set()
    while url:
        response = fetcher.get(url)
        page = _json(url, response, "tree response")
        if not isinstance(page, list):
            raise FetchError(url, "tree response is not a list")
        for value in page:
            entry = _tree_entry(url, value)
            if entry is None:
                continue
            if entry.path in seen_paths:
                raise FetchError(url, f"tree lists file path {entry.path!r} more than once")
            seen_paths.add(entry.path)
            entries.append(entry)
        next_url = next_link(response.headers)
        url = urljoin(url, next_url) if next_url else ""
    if not entries:
        raise FetchError(_tree_url(repo, revision), "tree has no usable file entries")
    return tuple(entries)


def hash_file(
    fetcher: Fetcher,
    repo: str,
    revision: str,
    path: str,
    expected_size: int,
) -> str:
    """Download and sha256-hash one non-LFS file from the Hub."""

    url = _file_url(repo, revision, path)
    response = fetcher.get(url)
    if not 200 <= response.status < 300:
        raise FetchError(url, _http_reason(response))
    actual_size = len(response.body)
    if actual_size != expected_size:
        raise FetchError(
            url,
            f"downloaded {actual_size} bytes but the tree records {expected_size}",
        )
    return f"sha256:{hashlib.sha256(response.body).hexdigest()}"


def record_files(
    fetcher: Fetcher, repo: str, revision: str
) -> dict[str, dict[str, object]]:
    """Build the sorted ``models.lock`` files mapping for a Hub revision."""

    entries = sorted(list_tree(fetcher, repo, revision), key=lambda entry: entry.path)
    return {
        entry.path: {
            "sha256": entry.sha256
            or hash_file(fetcher, repo, revision, entry.path, entry.size),
            "size": entry.size,
        }
        for entry in entries
    }


def _plain_yaml_key(key: str) -> bool:
    try:
        document = yaml.safe_load(f"{key}: null")
    except yaml.YAMLError:
        return False
    return (
        isinstance(document, Mapping)
        and len(document) == 1
        and next(iter(document)) == key
    )


def render_files_block(
    indent: int, files: Mapping[str, Mapping[str, object]]
) -> list[str]:
    """Render file entries at *indent* spaces in lock-file order."""

    lines: list[str] = []
    prefix = " " * indent
    value_indent = " " * (indent + 2)
    for path, entry in files.items():
        key = path if _plain_yaml_key(path) else json.dumps(path)
        lines.append(f"{prefix}{key}:")
        lines.append(f"{value_indent}sha256: {entry['sha256']}")
        lines.append(f"{value_indent}size: {entry['size']}")
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tools.pinwatch.hub")
    parser.add_argument("repo")
    parser.add_argument("revision", nargs="?")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print a model's files block for pasting into ``models.lock``."""

    options = _parser().parse_args(argv)
    fetcher = UrllibFetcher()
    try:
        revision = options.revision or model_head(fetcher, options.repo)
        files = record_files(fetcher, options.repo, revision)
    except FetchError as error:
        print(f"pin watch: {error} Fix: {_FIX}", file=sys.stderr)
        return 1
    print(" " * _FILES_INDENT + "files:")
    print("\n".join(render_files_block(_FILE_PATH_INDENT, files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
