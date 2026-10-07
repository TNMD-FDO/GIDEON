"""Read apt sources, including flat repositories, before writing an entry.

Two entries for one repository under different keys can break apt for the
whole host, so callers can inspect existing entries and unreadable files.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from gideon.host.sysio import Host

_SOURCES_LIST = "/etc/apt/sources.list"
_SOURCES_DIR = "/etc/apt/sources.list.d"
_LIST_LINE = re.compile(r"^(deb(?:-src)?)\s+(?:\[([^\]]*)\]\s+)?(\S+)\s+(\S+)(?:\s+(.+))?$")
_FIELD = re.compile(r"^([^\s:]+):\s*(.*)$")
# The words apt's boolean option parser reads as false.
_FALSE = frozenset({"no", "false", "without", "off", "disable", "0"})


@dataclass(frozen=True)
class Entry:
    """One apt source entry with its fields as written in the source file."""

    file: str
    form: str
    types: tuple[str, ...]
    uris: tuple[str, ...]
    suites: tuple[str, ...]
    components: tuple[str, ...]
    architectures: tuple[str, ...]
    signed_by: str | None
    enabled: bool


@dataclass(frozen=True)
class Scan:
    """Matching enabled entries and files that could not be read."""

    entries: tuple[Entry, ...]
    unreadable: tuple[tuple[str, str], ...]


def parse_list(text: str, file: str) -> tuple[Entry, ...]:
    """Parse the source lines apt considers in a one-line source file."""

    entries: list[Entry] = []
    for raw_line in text.splitlines():
        # apt ends a one-line entry at its first "#".
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        match = _LIST_LINE.fullmatch(line)
        if match is None:
            continue
        kind, option_text, uri, suite, component_text = match.groups()
        if uri.startswith("["):
            continue
        components = tuple(component_text.split()) if component_text else ()
        if not components and not suite.endswith("/"):
            continue
        options: dict[str, str] = {}
        if option_text is not None:
            valid = True
            for option in option_text.split():
                key, separator, value = option.partition("=")
                if not separator or not key or not value:
                    valid = False
                    break
                options[key.lower()] = value
            if not valid:
                continue
        entries.append(
            Entry(
                file=file,
                form="list",
                types=(kind,),
                uris=(uri,),
                suites=(suite,),
                components=components,
                architectures=tuple(options["arch"].split(","))
                if "arch" in options
                else (),
                signed_by=options.get("signed-by"),
                enabled=True,
            )
        )
    return tuple(entries)


def _stanza(fields: dict[str, str], file: str) -> Entry | None:
    types = tuple(fields.get("types", "").split())
    uris = tuple(fields.get("uris", "").split())
    if not types or not uris:
        return None
    return Entry(
        file=file,
        form="deb822",
        types=types,
        uris=uris,
        suites=tuple(fields.get("suites", "").split()),
        components=tuple(fields.get("components", "").split()),
        architectures=tuple(fields.get("architectures", "").split()),
        signed_by=fields.get("signed-by"),
        enabled=fields.get("enabled", "yes").strip().lower() not in _FALSE,
    )


def parse_deb822(text: str, file: str) -> tuple[Entry, ...]:
    """Parse source stanzas, preserving continuation text in field values."""

    entries: list[Entry] = []
    fields: dict[str, str] = {}
    previous: str | None = None
    for line in (*text.splitlines(), ""):
        if not line.strip():
            entry = _stanza(fields, file)
            if entry is not None:
                entries.append(entry)
            fields = {}
            previous = None
            continue
        if line.startswith("#"):
            continue
        if line[0].isspace():
            if previous is not None:
                fields[previous] += "\n" + line
            continue
        match = _FIELD.fullmatch(line)
        if match is None:
            previous = None
            continue
        previous = match.group(1).lower()
        fields[previous] = match.group(2)
    return tuple(entries)


def same_repository(uri: str, repository: str) -> bool:
    """Compare repository URIs with scheme and host case folded."""

    def comparable(value: str) -> str:
        parts = urlsplit(value)
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/"),
                parts.query,
                parts.fragment,
            )
        )

    try:
        return comparable(uri) == comparable(repository)
    except ValueError:
        return False


def entries_for(host: Host, repository: str) -> Scan:
    """Find enabled apt entries for a repository through the host seam."""

    entries: list[Entry] = []
    unreadable: list[tuple[str, str]] = []
    paths: list[str] = []
    if host.exists(_SOURCES_LIST):
        paths.append(_SOURCES_LIST)
    try:
        names = host.listdir(_SOURCES_DIR)
    except FileNotFoundError:
        names = []
    except (OSError, UnicodeError) as exc:
        unreadable.append((_SOURCES_DIR, str(exc)))
        names = []
    paths.extend(
        str(Path(_SOURCES_DIR) / name)
        for name in sorted(names)
        if name.endswith((".list", ".sources"))
    )
    for path in paths:
        try:
            contents = host.read_text(path)
        except (OSError, UnicodeError) as exc:
            unreadable.append((path, str(exc)))
            continue
        parser = parse_deb822 if path.endswith(".sources") else parse_list
        entries.extend(
            entry
            for entry in parser(contents, path)
            if entry.enabled
            and any(same_repository(uri, repository) for uri in entry.uris)
        )
    return Scan(tuple(entries), tuple(unreadable))
