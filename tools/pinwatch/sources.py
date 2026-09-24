"""Fetch and parse the non-registry upstreams watched by the pin tool."""

import json
import os
import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Final
from urllib.parse import quote

from tools.pinwatch.fetch import Fetcher, FetchError, Response


@dataclass(frozen=True, slots=True)
class GithubRelease:
    """The small, trusted subset of a GitHub release response we use."""

    tag: str
    body: str


@dataclass(frozen=True, slots=True)
class GithubCommit:
    """The full sha and committer timestamp of a GitHub branch head."""

    sha: str
    date: str


@dataclass(frozen=True, slots=True)
class CloudImage:
    """A dated Ubuntu cloud image and its published checksum."""

    url: str
    sha256: str


@total_ordering
@dataclass(frozen=True, slots=True)
class DebianVersion:
    """A Debian version split into epoch, upstream, and revision."""

    epoch: int
    upstream: str
    revision: str

    @classmethod
    def parse(cls, text: str) -> "DebianVersion":
        """Parse the Debian version grammar used by an apt Packages index."""

        if not text:
            raise ValueError("a Debian version must not be empty")
        epoch_text, separator, remainder = text.partition(":")
        if separator:
            if not epoch_text.isdigit():
                raise ValueError(f"invalid Debian epoch in {text!r}")
            epoch = int(epoch_text)
        else:
            epoch = 0
            remainder = text
        upstream, separator, revision = remainder.rpartition("-")
        if not separator:
            upstream, revision = remainder, ""
        if not upstream or (separator and not revision):
            raise ValueError(f"invalid Debian version {text!r}")
        return cls(epoch, upstream, revision)

    def _compare(self, other: "DebianVersion") -> int:
        if self.epoch != other.epoch:
            return -1 if self.epoch < other.epoch else 1
        upstream = _compare_debian_part(self.upstream, other.upstream)
        if upstream:
            return upstream
        return _compare_debian_part(self.revision, other.revision)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DebianVersion) and self._compare(other) == 0

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, DebianVersion):
            return NotImplemented
        return self._compare(other) < 0


def _non_digit_order(character: str | None) -> tuple[int, int]:
    """Return dpkg's order for one non-digit character or the string end."""

    if character == "~":
        return (0, 0)
    if character is None:
        return (1, 0)
    if character.isalpha():
        return (2, ord(character))
    return (3, ord(character))


def _compare_non_digits(left: str, right: str) -> int:
    index = 0
    while True:
        left_character = left[index] if index < len(left) else None
        right_character = right[index] if index < len(right) else None
        if left_character == right_character:
            if left_character is None:
                return 0
            index += 1
            continue
        left_order = _non_digit_order(left_character)
        right_order = _non_digit_order(right_character)
        return -1 if left_order < right_order else 1


def _compare_debian_part(left: str, right: str) -> int:
    left_index = right_index = 0
    while left_index < len(left) or right_index < len(right):
        left_end = left_index
        while left_end < len(left) and not left[left_end].isdigit():
            left_end += 1
        right_end = right_index
        while right_end < len(right) and not right[right_end].isdigit():
            right_end += 1
        non_digits = _compare_non_digits(
            left[left_index:left_end], right[right_index:right_end]
        )
        if non_digits:
            return non_digits
        left_index, right_index = left_end, right_end

        left_end = left_index
        while left_end < len(left) and left[left_end].isdigit():
            left_end += 1
        right_end = right_index
        while right_end < len(right) and right[right_end].isdigit():
            right_end += 1
        left_digits = left[left_index:left_end].lstrip("0") or "0"
        right_digits = right[right_index:right_end].lstrip("0") or "0"
        if len(left_digits) != len(right_digits):
            return -1 if len(left_digits) < len(right_digits) else 1
        if left_digits != right_digits:
            return -1 if left_digits < right_digits else 1
        left_index, right_index = left_end, right_end
    return 0


def debian_version_key(text: str) -> DebianVersion:
    """Return the total-order key for a Debian package version."""

    return DebianVersion.parse(text)


def newest(versions: tuple[str, ...] | list[str]) -> str:
    """Return the greatest Debian version from a non-empty sequence."""

    if not versions:
        raise ValueError("cannot choose the newest version from an empty sequence")
    return max(versions, key=debian_version_key)


_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_RUNNER_SHA: Final = re.compile(
    r"<!-- BEGIN SHA linux-x64 -->\s*([0-9a-f]{64})\s*"
    r"<!-- END SHA linux-x64 -->",
    re.DOTALL,
)
_DATE_ANCHOR: Final = re.compile(r'href=["\'](\d{8})/["\']')
_CLOUD_URL: Final = re.compile(
    r"^https://cloud-images\.ubuntu\.com/([^/]+)/([0-9]{8})/([^/?#]+)$"
)
# Ubuntu writes "<sha256> *<file>"; the two-space text form is accepted too.
_CHECKSUM_LINE: Final = re.compile(r"^([0-9a-f]{64}) [ *](.+)$", re.MULTILINE)


def _require_success(url: str, response: Response) -> None:
    if not 200 <= response.status < 300:
        raise FetchError(url, f"HTTP status {response.status}")


def _text(url: str, response: Response) -> str:
    _require_success(url, response)
    try:
        return response.body.decode("utf-8")
    except UnicodeDecodeError:
        raise FetchError(url, "response is not valid UTF-8") from None


def github_latest_release(
    fetcher: Fetcher,
    repository: str,
    *,
    token: str | None = None,
) -> GithubRelease:
    """Fetch the latest non-draft, non-prerelease release for *repository*."""

    url = f"https://api.github.com/repos/{repository}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    # Read the environment at call time.  This is intentionally not a default
    # expression, so tests and callers can supply a token explicitly.
    if token is None:
        token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = fetcher.get(url, headers=headers)
    body = _text(url, response)
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        raise FetchError(url, "response is not valid JSON") from None
    if not isinstance(document, dict):
        raise FetchError(url, "release response is not an object")
    tag = document.get("tag_name")
    release_body = document.get("body", "")
    if release_body is None:
        release_body = ""
    if not isinstance(tag, str) or not tag:
        raise FetchError(url, "release response has no tag_name")
    if not isinstance(release_body, str):
        raise FetchError(url, "release response has a non-text body")
    return GithubRelease(tag, release_body)


def github_branch_head(
    fetcher: Fetcher,
    repository: str,
    branch: str,
    *,
    token: str | None = None,
) -> GithubCommit:
    """Fetch and validate a GitHub branch's full head sha and commit date."""

    url = f"https://api.github.com/repos/{repository}/commits/{branch}"
    headers = {"Accept": "application/vnd.github+json"}
    if token is None:
        token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = _text(url, fetcher.get(url, headers=headers))
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        raise FetchError(url, "response is not valid JSON") from None
    if not isinstance(document, dict):
        raise FetchError(url, "commit response is not an object")
    sha = document.get("sha")
    commit = document.get("commit")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise FetchError(url, "commit response has no valid 40-hex sha")
    if not isinstance(commit, dict):
        raise FetchError(url, "commit response has no commit object")
    committer = commit.get("committer")
    if not isinstance(committer, dict):
        raise FetchError(url, "commit response has no committer object")
    date = committer.get("date")
    if not isinstance(date, str):
        raise FetchError(url, "commit response has no text committer date")
    return GithubCommit(sha, date)


def runner_sha256(body: str) -> str | None:
    """Extract the linux-x64 runner checksum marker, if present."""

    match = _RUNNER_SHA.search(body)
    return match.group(1) if match else None


def version_tuple(text: str, prefix_pattern: str = "") -> tuple[int, ...] | None:
    """Parse a dotted numeric version after a caller-supplied prefix regex."""

    match = re.compile(prefix_pattern).match(text)
    if match is None:
        return None
    remainder = text[match.end() :]
    if re.fullmatch(r"\d+(?:\.\d+){1,2}", remainder) is None:
        return None
    return tuple(int(part) for part in remainder.split("."))


def nvidia_branches(fetcher: Fetcher, repository: str) -> tuple[int, ...]:
    """Return distinct NVIDIA pinning branches from the apt ``Packages`` index."""

    url = f"https://developer.download.nvidia.com/compute/cuda/repos/{repository}/Packages"
    body = _text(url, fetcher.get(url))
    branches = {
        int(match.group(1))
        for match in re.finditer(
            r"^Package: nvidia-driver-pinning-(\d+)$", body, re.MULTILINE
        )
    }
    if not branches:
        # An empty or reshaped index must never read as "no newer branch".
        raise FetchError(url, "Packages index lists no nvidia-driver-pinning-<NNN> package")
    return tuple(sorted(branches))


def apt_package_versions(
    fetcher: Fetcher, apt_index: str, package: str
) -> tuple[str, ...]:
    """Return every version stanza for *package* in an apt Packages index."""

    body = _text(apt_index, fetcher.get(apt_index))
    versions: list[str] = []
    for stanza in re.split(r"\n[ \t]*\n", body.replace("\r\n", "\n")):
        fields: dict[str, list[str]] = {}
        for line in stanza.splitlines():
            if not line or line[0].isspace() or ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields.setdefault(key, []).append(value.strip())
        if fields.get("Package") == [package]:
            versions.extend(fields.get("Version", ()))
    if not versions:
        raise FetchError(apt_index, f"Packages index has no stanza for package {package}")
    return tuple(versions)


def ubuntu_cloud_image(fetcher: Fetcher, url: str) -> CloudImage | None:
    """Resolve the newest dated image in the pinned Ubuntu series."""

    match = _CLOUD_URL.fullmatch(url)
    if match is None:
        raise FetchError(url, "pinned cloud-image URL has an unexpected shape")
    series, current_date, filename = match.groups()
    listing_url = f"https://cloud-images.ubuntu.com/{series}/"
    listing = _text(listing_url, fetcher.get(listing_url))
    dates = sorted(set(_DATE_ANCHOR.findall(listing)))
    if not dates:
        raise FetchError(listing_url, "cloud-image listing has no dated directories")
    newest = dates[-1]
    if newest <= current_date:
        return None

    sums_url = f"https://cloud-images.ubuntu.com/{series}/{newest}/SHA256SUMS"
    sums = _text(sums_url, fetcher.get(sums_url))
    checksum: str | None = None
    for checksum_match in _CHECKSUM_LINE.finditer(sums):
        if checksum_match.group(2) == filename:
            checksum = checksum_match.group(1)
            break
    if checksum is None or _SHA256.fullmatch(checksum) is None:
        raise FetchError(sums_url, f"SHA256SUMS has no checksum for {filename}")
    new_url = f"https://cloud-images.ubuntu.com/{series}/{newest}/{quote(filename)}"
    return CloudImage(new_url, checksum)
