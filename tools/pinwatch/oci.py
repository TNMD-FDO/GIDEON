"""Anonymous OCI registry operations and the pin watch tag rule."""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from gideon.host.images import DIGEST, SOURCE_REFERENCE
from tools.pinwatch.fetch import Fetcher, FetchError, Response, next_link


@dataclass(frozen=True, slots=True)
class Reference:
    """A normalized, tagged OCI image reference."""

    registry: str
    repository: str
    tag: str
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class TagShape:
    """The components and grammar that make an upstream tag comparable."""

    prefix: str
    components: tuple[int, ...]
    suffix: str = ""
    separators: tuple[str, ...] = ()
    kind: Literal["release", "dated-commit"] = "release"


@dataclass(frozen=True, slots=True)
class UnreadableTagError(ValueError):
    """A current tag has no shape; remain a ``ValueError`` for resolver callers."""

    tag: str
    problem: str
    fix: str

    def __post_init__(self) -> None:
        ValueError.__init__(self, self.problem)


@dataclass(frozen=True, slots=True)
class BearerChallenge:
    """The anonymous token endpoint described by a registry challenge."""

    realm: str
    service: str | None = None
    scope: str | None = None


_TAG: Final = re.compile(
    r"^(?P<prefix>v)?(?P<numeric>\d+(?:[.-]\d+)*)(?:-(?P<suffix>"
    r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z][A-Za-z0-9]*)*"
    r"))?$"
)
_DATED_COMMIT: Final = re.compile(
    r"^(?P<year>\d{4})\.(?P<month>\d{1,2})\.(?P<day>\d{1,2})-"
    r"(?P<commit>[0-9a-f]{7,40})$"
)
_BEARER_PARAM: Final = re.compile(
    r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:\"([^\"]*)\"|([^,\s]+))"
)
_MANIFEST_ACCEPT: Final = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


def _header(response: Response, name: str) -> str | None:
    return response.headers.get(name)  # Response headers are case-insensitive


def _response_error(url: str, response: Response) -> FetchError:
    return FetchError(url, f"HTTP status {response.status}")


def parse_reference(source: str) -> Reference:
    """Parse a lock-file image source, including ``name:tag@digest``."""

    digest: str | None = None
    name_and_tag = source
    if "@" in source:
        name_and_tag, digest = source.rsplit("@", 1)
        if DIGEST.fullmatch(digest) is None:
            raise ValueError(f"invalid image digest in {source!r}")
    if SOURCE_REFERENCE.fullmatch(name_and_tag) is None:
        raise ValueError(f"invalid tagged image reference: {source!r}")

    slash = name_and_tag.rfind("/")
    colon = name_and_tag.rfind(":")
    if colon <= slash:
        raise ValueError(f"image reference has no tag: {source!r}")
    name = name_and_tag[:colon]
    tag = name_and_tag[colon + 1 :]
    first, separator, repository = name.partition("/")
    has_registry = bool(separator) and (
        "." in first or ":" in first or first == "localhost"
    )
    if has_registry:
        registry = first
    else:
        registry = "docker.io"
        repository = name
    if registry == "docker.io":
        registry = "registry-1.docker.io"
        if "/" not in repository:
            repository = f"library/{repository}"
    return Reference(registry, repository, tag, digest)


def bearer_challenge(response: Response) -> BearerChallenge | None:
    """Parse a ``Bearer`` challenge, returning ``None`` for other responses."""

    if response.status != 401:
        return None
    value = _header(response, "WWW-Authenticate")
    if value is None:
        return None
    scheme, _, parameters = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    values: dict[str, str] = {}
    for match in _BEARER_PARAM.finditer(parameters):
        values[match.group(1).lower()] = match.group(2) or match.group(3) or ""
    realm = values.get("realm")
    if not realm:
        return None
    return BearerChallenge(realm, values.get("service"), values.get("scope"))


def _token_url(challenge: BearerChallenge) -> str:
    parsed = urlsplit(challenge.realm)
    query = list(parse_qsl(parsed.query, keep_blank_values=True))
    if challenge.service is not None:
        query.append(("service", challenge.service))
    if challenge.scope is not None:
        query.append(("scope", challenge.scope))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _authorized_get(
    fetcher: Fetcher,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
) -> Response:
    """Perform one anonymous request, satisfying one bearer challenge."""

    initial = fetcher.get(url, headers=headers, method=method)
    if initial.status != 401:
        return initial
    challenge = bearer_challenge(initial)
    if challenge is None:
        raise FetchError(url, "HTTP 401 without a usable bearer challenge")
    token_response = fetcher.get(_token_url(challenge), headers=None)
    if not 200 <= token_response.status < 300:
        raise _response_error(challenge.realm, token_response)
    try:
        token_document = json.loads(token_response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(
            challenge.realm, f"invalid bearer token response: {exc}"
        ) from None
    token = token_document.get("token") if isinstance(token_document, dict) else None
    if not isinstance(token, str) or not token:
        token = (
            token_document.get("access_token")
            if isinstance(token_document, dict)
            else None
        )
    if not isinstance(token, str) or not token:
        raise FetchError(challenge.realm, "bearer token response has no token")
    authorized_headers = dict(headers or {})
    authorized_headers["Authorization"] = f"Bearer {token}"
    retried = fetcher.get(url, headers=authorized_headers, method=method)
    if retried.status == 401:
        raise FetchError(url, "HTTP 401 after bearer token retry")
    return retried


def _tags_url(reference: Reference) -> str:
    repository = quote(reference.repository, safe="/")
    return f"https://{reference.registry}/v2/{repository}/tags/list?n=1000"


def list_tags(fetcher: Fetcher, reference: Reference) -> tuple[str, ...]:
    """Return all tags, following OCI ``Link: rel=next`` pages."""

    url = _tags_url(reference)
    tags: list[str] = []
    while url:
        response = _authorized_get(fetcher, url)
        if not 200 <= response.status < 300:
            raise _response_error(url, response)
        try:
            document = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError(url, f"invalid tags response: {exc}") from None
        page_tags = document.get("tags") if isinstance(document, dict) else None
        if not isinstance(page_tags, list) or any(
            not isinstance(tag, str) for tag in page_tags
        ):
            raise FetchError(url, "tags response has no string tags list")
        tags.extend(page_tags)
        next_url = next_link(response.headers)
        url = urljoin(url, next_url) if next_url else ""
    return tuple(tags)


def resolve_digest(fetcher: Fetcher, reference: Reference, tag: str) -> str:
    """Resolve a tag to its OCI index or manifest digest using ``HEAD``."""

    repository = quote(reference.repository, safe="/")
    encoded_tag = quote(tag, safe="")
    url = f"https://{reference.registry}/v2/{repository}/manifests/{encoded_tag}"
    response = _authorized_get(
        fetcher,
        url,
        headers={"Accept": _MANIFEST_ACCEPT},
        method="HEAD",
    )
    if not 200 <= response.status < 300:
        raise _response_error(url, response)
    digest = _header(response, "Docker-Content-Digest")
    if digest is None:
        raise FetchError(url, "response has no Docker-Content-Digest header")
    if DIGEST.fullmatch(digest) is None:
        raise FetchError(url, "Docker-Content-Digest is not a valid sha256 digest")
    return digest


def tag_shape(tag: str) -> TagShape | None:
    """Parse a release or SearXNG dated-commit tag; ``None`` otherwise.

    Numeric groups may use dots or dashes, and a final suffix may be one word
    or several dash-joined words.  The suffix is retained as one string and
    compared whole by ``same_shape``.  A word starts with a letter, so the
    numeric group and suffix never compete.  The separators are retained
    because they are part of a pin's shape.  The dated-commit grammar belongs
    to SearXNG's published tags
    (docs/research/searxng-service-and-owui-search.md §1); its commit hash is
    not part of the shape.
    """

    dated = _DATED_COMMIT.fullmatch(tag)
    if dated is not None:
        return TagShape(
            "",
            tuple(
                int(dated.group(name)) for name in ("year", "month", "day")
            ),
            "",
            (".", "."),
            "dated-commit",
        )
    match = _TAG.fullmatch(tag)
    if match is None:
        return None
    numeric = match.group("numeric")
    return TagShape(
        match.group("prefix") or "",
        tuple(int(part) for part in re.split(r"[.-]", numeric)),
        match.group("suffix") or "",
        tuple(re.findall(r"[.-]", numeric)),
        "release",
    )


def same_shape(current_tag: str, candidate: str) -> bool:
    """True when both tags have the same upstream tag shape."""

    current_shape = tag_shape(current_tag)
    candidate_shape = tag_shape(candidate)
    if current_shape is None or candidate_shape is None:
        return False
    if current_shape.kind != candidate_shape.kind:
        return False
    if current_shape.kind == "dated-commit":
        return True
    return (
        current_shape.prefix == candidate_shape.prefix
        and current_shape.suffix == candidate_shape.suffix
        and current_shape.separators == candidate_shape.separators
        and len(current_shape.components) == len(candidate_shape.components)
    )


def _unreadable_tag(current_tag: str) -> UnreadableTagError:
    return UnreadableTagError(
        current_tag,
        f"current tag has no shape that tag_shape reads: {current_tag!r}",
        "Teach tag_shape in tools/pinwatch/oci.py the tag's grammar, "
        "then re-run the pin watch.",
    )


def newest_same_shape_candidates(
    tags: Iterable[str], current_tag: str
) -> tuple[str, ...]:
    """Return the tags at the greatest shape value that could replace *current_tag*.

    A release tag must be strictly greater.  A dated-commit tag's date is
    not its whole identity — SearXNG builds more than once on many days —
    so another hash on the pin's own date is a candidate too, and the caller
    breaks the tie among the candidates and the pin by creation time.
    """

    current_shape = tag_shape(current_tag)
    if current_shape is None:
        raise _unreadable_tag(current_tag)
    newer: list[tuple[str, TagShape]] = []
    for tag in tags:
        shape = tag_shape(tag)
        if shape is None or not same_shape(current_tag, tag) or tag == current_tag:
            continue
        if shape.components > current_shape.components or (
            current_shape.kind == "dated-commit"
            and shape.components == current_shape.components
        ):
            newer.append((tag, shape))
    if not newer:
        return ()
    greatest = max(shape.components for _, shape in newer)
    if current_shape.kind == "dated-commit":
        return tuple(tag for tag, shape in newer if shape.components == greatest)
    return (next(tag for tag, shape in newer if shape.components == greatest),)


def newest_same_shape(tags: Iterable[str], current_tag: str) -> str:
    """Choose the greatest tag with the current tag's exact shape."""

    current_shape = tag_shape(current_tag)
    if current_shape is None:
        raise _unreadable_tag(current_tag)
    candidates = newest_same_shape_candidates(tags, current_tag)
    if not candidates:
        return current_tag
    return candidates[0]


def leading_component_changed(current_tag: str, candidate: str) -> bool:
    """Report whether two same-shape tags change their leading component."""

    current_shape = tag_shape(current_tag)
    candidate_shape = tag_shape(candidate)
    if (
        current_shape is not None
        and candidate_shape is not None
        and (
            current_shape.kind == "dated-commit"
            or candidate_shape.kind == "dated-commit"
        )
    ):
        return False
    return bool(
        current_shape
        and candidate_shape
        and len(current_shape.components) == len(candidate_shape.components)
        and current_shape.components[0] != candidate_shape.components[0]
    )


def _manifest_url(reference: Reference, tag_or_digest: str) -> str:
    repository = quote(reference.repository, safe="/")
    value = quote(tag_or_digest, safe=":")
    return f"https://{reference.registry}/v2/{repository}/manifests/{value}"


def _json_document(
    fetcher: Fetcher,
    url: str,
    tag: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    response = _authorized_get(fetcher, url, headers=headers)
    if not 200 <= response.status < 300:
        raise _response_error(url, response)
    try:
        document = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(url, f"tag {tag!r} has invalid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise FetchError(url, f"tag {tag!r} manifest is not an object")
    return document


def _is_linux_amd64(platform: object) -> bool:
    """The index entry the box runs; a variant (arm/v7's) is never one."""

    return (
        isinstance(platform, dict)
        and platform.get("os") == "linux"
        and platform.get("architecture") == "amd64"
        and not platform.get("variant")
    )


def image_created(fetcher: Fetcher, reference: Reference, tag: str) -> str:
    """Return a tag's registry-recorded configuration creation time.

    The manifest/index walk is the same-date tie-break for SearXNG's published
    tags (docs/research/searxng-service-and-owui-search.md §1).
    """

    manifest_url = _manifest_url(reference, tag)
    manifest = _json_document(
        fetcher, manifest_url, tag, headers={"Accept": _MANIFEST_ACCEPT}
    )
    manifests = manifest.get("manifests")
    if isinstance(manifests, list):
        amd64: Mapping[str, object] | None = None
        for entry in manifests:
            if not isinstance(entry, dict):
                continue
            if _is_linux_amd64(entry.get("platform")):
                amd64 = entry
                break
        digest = amd64.get("digest") if amd64 is not None else None
        if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
            raise FetchError(
                manifest_url,
                f"tag {tag!r} has no linux/amd64 manifest entry",
            )
        manifest_url = _manifest_url(reference, digest)
        manifest = _json_document(
            fetcher, manifest_url, tag, headers={"Accept": _MANIFEST_ACCEPT}
        )

    config = manifest.get("config")
    digest = config.get("digest") if isinstance(config, dict) else None
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise FetchError(manifest_url, f"tag {tag!r} has no config digest")
    repository = quote(reference.repository, safe="/")
    blob_url = f"https://{reference.registry}/v2/{repository}/blobs/{quote(digest, safe=':')}"
    blob = _authorized_get(fetcher, blob_url)
    if not 200 <= blob.status < 300:
        raise _response_error(blob_url, blob)
    try:
        config_document = json.loads(blob.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchError(blob_url, f"tag {tag!r} has invalid config JSON: {exc}") from None
    created = (
        config_document.get("created")
        if isinstance(config_document, dict)
        else None
    )
    if not isinstance(created, str):
        raise FetchError(blob_url, f"tag {tag!r} has no created time")
    return created


def created_time(text: str) -> datetime:
    """Parse a registry configuration's RFC 3339 creation time."""

    try:
        value = datetime.fromisoformat(text)
        if value.tzinfo is None:
            raise ValueError("timestamp has no timezone")
        return value
    except ValueError:
        raise FetchError("created", f"unparseable created time {text!r}") from None
