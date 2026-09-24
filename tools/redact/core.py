"""Core redaction for secrets and registry-marked office values."""

import ipaddress
import keyword
import re
from collections.abc import Callable, Iterator
from typing import Final

from gideon.host.apply import PRINT_ONCE_LINE
from gideon.host.site import (
    FIELD_REGISTRY,
    GROUP_PATHS,
    SiteConfig,
    group_name,
    parse_group_dn,
)
from gideon.host.steps.site_dirs import AGE_IDENTITY_LINE, AGE_SECRET_PREFIX

REDACTED: Final = "<redacted>"
_AGE_SECRET_KEY: Final = re.compile(
    re.escape(AGE_SECRET_PREFIX) + rf"(?!{re.escape(REDACTED)})[0-9A-Za-z]*"
)
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    r"<(?:[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*(?:\[[0-9]+\])?"
    r"|address in lan_cidrs\[[0-9]+\]|redacted)>"
)
_IPV4: Final[re.Pattern[str]] = re.compile(
    r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/[0-9]{1,2})?(?![0-9])"
)


def _replace_secret_value(line: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(line)
    if match is None:
        return line
    value = match.group("value")
    if value == REDACTED:
        return line
    return line[: match.start("value")] + REDACTED + line[match.end("value") :]


def _redact_secrets(text: str) -> str:
    """Replace the product's print-once secrets before captured text is stored.

    The shapes are the product's own (apply's break-glass line, the age
    identity announcement), imported so the caller never guesses; any
    ``AGE-SECRET-KEY-1`` token is masked whatever follows it, because a
    transcript is never illustrative.
    """

    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        line = _replace_secret_value(line, PRINT_ONCE_LINE)
        line = _replace_secret_value(line, AGE_IDENTITY_LINE)
        line = _AGE_SECRET_KEY.sub(AGE_SECRET_PREFIX + REDACTED, line)
        lines.append(line)
    return "".join(lines)


def _attribute_name(segment: str) -> str:
    return segment + "_" if keyword.iskeyword(segment) else segment


def _resolved_value(site: SiteConfig, path: str) -> object:
    current: object = site
    for segment in path.split("."):
        current = getattr(current, _attribute_name(segment))
    return current


def _ignorable_address(value: str) -> bool:
    """True for a loopback or unspecified address, range, or ``address:port``."""

    host, _, port = value.rpartition(":")
    if host and port.isdigit() and "." in host:
        value = host
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            value
        )
    except ValueError:
        try:
            network = ipaddress.ip_network(value)
        except ValueError:
            return False
        return network.is_loopback or network.is_unspecified
    return address.is_loopback or address.is_unspecified


def _group_entries(
    placeholder: str, value: str, default: object | None
) -> Iterator[tuple[str, str]]:
    name = group_name(value)
    if "=" in value:
        # A spelled DN is the marked leaf's whole value, replaced whole — its
        # container names a directory object of the office — and its CN as
        # spelled, the escaped form directory tools print, beside the decoded
        # name when they differ.
        yield placeholder, value
        for attribute, component in parse_group_dn(value):
            if attribute == "cn" and component != name:
                yield placeholder, component
                break
    # The product's default group name is not an office value.
    if default is not None and name == default:
        return
    yield placeholder, name


def _value_entries(
    path: str, value: object, default: object | None, index: int | None = None
) -> Iterator[tuple[str, str]]:
    if not isinstance(value, str) or not value:
        # Empty values are not office evidence.
        return
    # Loopback and unspecified addresses locate no office.
    if _ignorable_address(value):
        return
    placeholder = f"<{path}{f'[{index}]' if index is not None else ''}>"
    if path in GROUP_PATHS:
        yield from _group_entries(placeholder, value, default)
        return
    yield placeholder, value


def _office_value_entries(site: SiteConfig) -> Iterator[tuple[str, str]]:
    """Yield placeholders and values for the registry's marked site leaves."""

    for spec in FIELD_REGISTRY:
        if not spec.office_value:
            continue
        value = _resolved_value(site, spec.path)
        if spec.default is not None and value == spec.default:
            # A registry default is product content, not office content.
            continue
        if isinstance(value, list):
            for index, item in enumerate(value):
                yield from _value_entries(spec.path, item, None, index)
        else:
            yield from _value_entries(spec.path, value, spec.default)


def _split_placeholder_spans(text: str) -> Iterator[tuple[str, bool]]:
    """Split text into protected redaction placeholders and ordinary spans."""

    # An existing placeholder is never matched again.
    cursor = 0
    for match in _PLACEHOLDER.finditer(text):
        if cursor < match.start():
            yield text[cursor : match.start()], False
        yield match.group(0), True
        cursor = match.end()
    if cursor < len(text):
        yield text[cursor:], False


def _replace_outside_placeholders(text: str, replace: Callable[[str], str]) -> str:
    pieces: list[str] = []
    for piece, protected in _split_placeholder_spans(text):
        pieces.append(piece if protected else replace(piece))
    return "".join(pieces)


def _office_value_pattern(entries: list[tuple[str, str]]) -> re.Pattern[str] | None:
    if not entries:
        return None
    values = sorted(entries, key=lambda entry: -len(entry[1]))
    alternatives = "|".join(re.escape(value) for _, value in values)
    # Match the longest bounded value, including a domain suffix.
    return re.compile(
        rf"(?<![\w-])(?:{alternatives})(?![\w-]|\.(?=[\w-]))",
        re.IGNORECASE,
    )


def _redact_office_values(text: str, site: SiteConfig) -> str:
    entries = list(_office_value_entries(site))
    pattern = _office_value_pattern(entries)
    if pattern is None:
        return text
    ordered = sorted(entries, key=lambda entry: -len(entry[1]))

    def replace(match: re.Match[str]) -> str:
        matched = match.group(0).casefold()
        for placeholder, value in ordered:
            if matched == value.casefold():
                return placeholder
        return match.group(0)

    return _replace_outside_placeholders(
        text, lambda segment: pattern.sub(replace, segment)
    )


def _lan_networks(site: SiteConfig) -> Iterator[tuple[int, ipaddress.IPv4Network]]:
    for index, value in enumerate(site.lan_cidrs):
        try:
            network = ipaddress.ip_network(value)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network):
            yield index, network


def _redact_in_range_addresses(text: str, site: SiteConfig) -> str:
    networks = tuple(_lan_networks(site))
    if not networks:
        return text

    def replace(segment: str) -> str:
        def replace_address(match: re.Match[str]) -> str:
            address_text, _, _ = match.group(0).partition("/")
            try:
                address = ipaddress.ip_address(address_text)
            except ValueError:
                return match.group(0)
            if not isinstance(address, ipaddress.IPv4Address):
                return match.group(0)
            for index, network in networks:
                if address in network:
                    # An address inside an office range gets its range placeholder.
                    return f"<address in lan_cidrs[{index}]>"
            return match.group(0)

        return _IPV4.sub(replace_address, segment)

    return _replace_outside_placeholders(text, replace)


def redact(text: str, site: SiteConfig | None = None) -> str:
    """Replace captured secrets and, when supplied, marked office values."""

    redacted = _redact_secrets(text)
    if site is None:
        return redacted
    redacted = _redact_office_values(redacted, site)
    return _redact_in_range_addresses(redacted, site)
