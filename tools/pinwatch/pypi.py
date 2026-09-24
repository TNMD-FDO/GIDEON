"""Read stable releases from PyPI's PEP 691 Index API for the pin watch.

The Index API is used instead of PyPI's project JSON because its ``releases``
field is deprecated.  The reader follows PEP 440's Appendix B parser, PEP 503's
normalized project names, PEP 592's per-file yanks, PEP 629's API versioning,
PEP 691's JSON representation, PEP 700's separate file/version lists, and
PEP 792's quarantined-project status, as described in the verified research
note.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import total_ordering
from typing import Final
from urllib.parse import quote

from tools.pinwatch.fetch import Fetcher, FetchError, Response

PYPI_INDEX_ROOT: Final = "https://pypi.org/simple"
PYPI_ACCEPT: Final = "application/vnd.pypi.simple.v1+json"
PYPI_API_MAJOR: Final = 1
PYPI_PROJECT_ROOT: Final = "https://pypi.org/project"

VERSION_PATTERN: Final = r"""
    v?
    (?:
        (?:(?P<epoch>[0-9]+)!)?                           # epoch
        (?P<release>[0-9]+(?:\.[0-9]+)*)                  # release segment
        (?P<pre>                                          # pre-release
            [-_\.]?
            (?P<pre_l>(a|b|c|rc|alpha|beta|pre|preview))
            [-_\.]?
            (?P<pre_n>[0-9]+)?
        )?
        (?P<post>                                         # post release
            (?:-(?P<post_n1>[0-9]+))
            |
            (?:
                [-_\.]?
                (?P<post_l>post|rev|r)
                [-_\.]?
                (?P<post_n2>[0-9]+)?
            )
        )?
        (?P<dev>                                          # dev release
            [-_\.]?
            (?P<dev_l>dev)
            [-_\.]?
            (?P<dev_n>[0-9]+)?
        )?
    )
    (?:\+(?P<local>[a-z0-9]+(?:[-_\.][a-z0-9]+)*))?       # local version
"""
_VERSION_RE: Final = re.compile(
    r"^\s*" + VERSION_PATTERN + r"\s*$", re.VERBOSE | re.IGNORECASE
)
_API_VERSION: Final = re.compile(r"^(\d+)\.(\d+)$")
_ARCHIVE_SUFFIXES: Final = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".zip",
    ".tgz",
    ".tar",
)
_PRE_RELEASES: Final = {
    "alpha": "a",
    "a": "a",
    "beta": "b",
    "b": "b",
    "preview": "rc",
    "pre": "rc",
    "c": "rc",
    "rc": "rc",
}


@dataclass(frozen=True, slots=True)
class _ParsedVersion:
    """The normalized components captured by the PEP 440 pattern."""

    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None
    post: int | None
    dev: int | None
    local: tuple[str | int, ...] | None


def _parsed_version(text: str) -> _ParsedVersion | None:
    match = _VERSION_RE.fullmatch(text)
    if match is None:
        return None
    groups = match.groupdict()
    release = tuple(int(part) for part in groups["release"].split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    pre_label = groups["pre_l"]
    pre = (
        (_PRE_RELEASES[pre_label.lower()], int(groups["pre_n"] or "0"))
        if pre_label is not None
        else None
    )
    post_text = groups["post_n1"] or groups["post_n2"]
    post = int(post_text or "0") if groups["post"] is not None else None
    dev = int(groups["dev_n"] or "0") if groups["dev_l"] is not None else None
    local_text = groups["local"]
    local = None
    if local_text is not None:
        local = tuple(
            int(part) if part.isdigit() else part.lower()
            for part in re.split(r"[-_.]", local_text)
        )
    return _ParsedVersion(
        epoch=int(groups["epoch"] or "0"),
        release=release,
        pre=pre,
        post=post,
        dev=dev,
        local=local,
    )


def _version_identity(text: str) -> tuple[object, ...] | None:
    """Return every normalized PEP 440 component of *text*, if valid."""

    parsed = _parsed_version(text)
    if parsed is None:
        return None
    return (
        parsed.epoch,
        parsed.release,
        parsed.pre,
        parsed.post,
        parsed.dev,
        parsed.local,
    )


@total_ordering
@dataclass(frozen=True, slots=True)
class StableVersion:
    """A final or post-release PEP 440 version used for candidate ordering."""

    epoch: int
    release: tuple[int, ...]
    post: int | None

    @classmethod
    def parse(cls, text: str) -> "StableVersion | None":
        """Parse a final or post release, ignoring pre/dev/local versions."""

        parsed = _parsed_version(text)
        if parsed is None or parsed.pre is not None or parsed.dev is not None:
            return None
        if parsed.local is not None:
            return None
        return cls(parsed.epoch, parsed.release, parsed.post)

    def _key(self) -> tuple[object, ...]:
        post = (0, 0) if self.post is None else (1, self.post)
        return (self.epoch, self.release, post)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StableVersion) and self._key() == other._key()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, StableVersion):
            return NotImplemented
        return self._key() < other._key()


def version_from_filename(filename: str, project: str) -> str | None:
    """Return the PEP 440 version attributed to a PyPI distribution filename."""

    lowered = filename.lower()
    project_pattern = re.escape(project).replace(r"\-", "[-_.]")
    prefix = re.match(rf"^{project_pattern}-", lowered)
    if prefix is None:
        return None
    remainder = lowered[prefix.end() :]
    if lowered.endswith(".whl"):
        version, separator, _ = remainder[:-4].partition("-")
        return version if separator and _version_identity(version) is not None else None
    suffix = next(
        (suffix for suffix in _ARCHIVE_SUFFIXES if lowered.endswith(suffix)), None
    )
    if suffix is None:
        return None
    version = remainder[: -len(suffix)]
    return version if version and _version_identity(version) is not None else None


def project_page(project: str, version: str) -> str:
    """Return the PyPI release page for *project* at *version*."""

    return (
        f"{PYPI_PROJECT_ROOT}/{quote(project, safe='')}/"
        f"{quote(version, safe='')}/"
    )


@dataclass(frozen=True, slots=True)
class _IndexFile:
    """The validated file fields used by the release walk."""

    filename: str
    yanked: bool


def _json(url: str, response: Response) -> object:
    if not 200 <= response.status < 300:
        raise FetchError(url, f"HTTP status {response.status}")
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FetchError(url, "response is not the PyPI JSON form") from None


def _api_version(url: str, value: object) -> None:
    if not isinstance(value, str):
        raise FetchError(url, "meta.api-version is not a major.minor string")
    match = _API_VERSION.fullmatch(value)
    if match is None:
        raise FetchError(url, f"meta.api-version has invalid value {value!r}")
    if int(match.group(1)) != PYPI_API_MAJOR:
        raise FetchError(url, f"unsupported Index API major in {value!r}")


def _index_files(url: str, value: object) -> tuple[_IndexFile, ...]:
    if not isinstance(value, list):
        raise FetchError(url, "files is not a list")
    files: list[_IndexFile] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise FetchError(url, f"files[{index}] is not an object")
        filename = item.get("filename")
        if not isinstance(filename, str):
            raise FetchError(url, f"files[{index}].filename is not a string")
        yanked = item.get("yanked", False)
        if not isinstance(yanked, (bool, str)):
            raise FetchError(url, f"files[{index}].yanked is not a boolean or string")
        files.append(_IndexFile(filename, bool(yanked)))
    return tuple(files)


def _index_document(
    url: str, document: object
) -> tuple[tuple[str, ...], tuple[_IndexFile, ...]]:
    if not isinstance(document, Mapping):
        raise FetchError(url, "Index API response is not an object")
    meta = document.get("meta")
    if not isinstance(meta, Mapping):
        raise FetchError(url, "meta is not an object")
    _api_version(url, meta.get("api-version"))
    versions = document.get("versions")
    if not isinstance(versions, list) or any(
        not isinstance(version, str) for version in versions
    ):
        raise FetchError(url, "versions is not a list of strings")
    files = _index_files(url, document.get("files"))
    project_status = document.get("project-status")
    if project_status is not None:
        if not isinstance(project_status, Mapping):
            raise FetchError(url, "project-status is not an object")
        status = project_status.get("status")
        if status == "quarantined":
            raise FetchError(url, "project is quarantined")
        if status is not None and not isinstance(status, str):
            raise FetchError(url, "project-status.status is not a string")
    return tuple(versions), files


def _listed_versions(
    url: str, versions: tuple[str, ...]
) -> dict[tuple[object, ...], str]:
    listed: dict[tuple[object, ...], str] = {}
    for version in versions:
        identity = _version_identity(version)
        if identity is None:
            continue
        if identity in listed:
            raise FetchError(
                url,
                "versions lists duplicate normalized releases "
                f"{listed[identity]!r} and {version!r}",
            )
        listed[identity] = version
    return listed


def pypi_version(fetcher: Fetcher, project: str, current: str) -> str | None:
    """Return PyPI's newest stable release above *current*, if one exists."""

    current_version = StableVersion.parse(current)
    if current_version is None:
        raise ValueError(f"current PyPI version {current!r} is not stable")
    url = f"{PYPI_INDEX_ROOT}/{project}/"
    response = fetcher.get(url, headers={"Accept": PYPI_ACCEPT})
    versions, files = _index_document(url, _json(url, response))
    listed = _listed_versions(url, versions)
    attributed: dict[tuple[object, ...], list[_IndexFile]] = {}
    unattributed: list[str] = []
    for file in files:
        version = version_from_filename(file.filename, project)
        if version is None:
            unattributed.append(file.filename)
            continue
        identity = _version_identity(version)
        if identity not in listed:
            raise FetchError(
                url,
                f"file {file.filename!r} names unlisted version {version!r}",
            )
        attributed.setdefault(identity, []).append(file)

    candidates = [
        (stable, listed[identity], identity)
        for identity in listed
        if (stable := StableVersion.parse(listed[identity])) is not None
        and stable > current_version
    ]
    for _, version, identity in sorted(
        candidates, key=lambda candidate: candidate[0], reverse=True
    ):
        if any(not file.yanked for file in attributed.get(identity, ())):
            return version
        if unattributed:
            examples = ", ".join(repr(name) for name in unattributed[:3])
            raise FetchError(
                url,
                f"passed over release {version!r} beside {len(unattributed)} "
                f"unattributed file(s): {examples}; attribution rule needs a ticket",
            )
    return None
