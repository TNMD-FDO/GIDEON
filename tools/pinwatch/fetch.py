"""Small, injectable HTTP transport for the pin watch."""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import Final, Protocol
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

_NEXT_LINK: Final = re.compile(
    r"<([^>]+)>\s*;[^,]*\brel\s*=\s*\"?next\"?\b", re.IGNORECASE
)


class CaseInsensitiveHeaders(Mapping[str, str]):
    """An immutable header mapping whose lookup ignores ASCII case."""

    def __init__(self, headers: Mapping[str, str]) -> None:
        self._values = {key.lower(): value for key, value in headers.items()}

    def __getitem__(self, key: str) -> str:
        return self._values[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True, slots=True)
class Response:
    """An HTTP response returned by a :class:`Fetcher`."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", CaseInsensitiveHeaders(self.headers))


@dataclass(frozen=True, slots=True)
class FetchError(Exception):
    """A transport or protocol refusal tied to one URL."""

    url: str
    reason: str

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.url}: {self.reason}")


class Fetcher(Protocol):
    """The only HTTP operation used by the watch."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        method: str = "GET",
    ) -> Response: ...


def next_link(headers: Mapping[str, str]) -> str | None:
    """Return the URL in a ``Link: rel=next`` header, if present."""

    link = next(
        (value for key, value in headers.items() if key.lower() == "link"),
        "",
    )
    match = _NEXT_LINK.search(link)
    return match.group(1) if match else None


class UrllibFetcher:
    """The production fetcher, with redirects and a bounded request time.

    ``proxies`` (scheme → proxy URL, the ``ProxyHandler`` shape) routes every
    request through an egress proxy; without it the process environment rules,
    as on the hosted runner.
    """

    timeout = 30.0

    def __init__(self, proxies: Mapping[str, str] | None = None) -> None:
        self._open = (
            build_opener(ProxyHandler(dict(proxies))).open if proxies else urlopen
        )

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        method: str = "GET",
    ) -> Response:
        request = Request(url, headers=dict(headers or {}), method=method)
        try:
            with self._open(request, timeout=self.timeout) as response:
                return Response(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except HTTPError as exc:
            try:
                body = exc.read()
            except (OSError, HTTPException):
                body = b""
            return Response(
                status=exc.code,
                headers=dict(exc.headers.items()),
                body=body,
            )
        except (OSError, HTTPException) as exc:
            # URLError and socket timeouts are OSErrors; a truncated body is an
            # http.client.IncompleteRead. Either is one failed row, never a crash.
            raise FetchError(url, str(exc)) from None
