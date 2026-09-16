"""Shared LDAPS command and LDIF helpers for the host path.

The directory password is always passed as a file path.  Its contents never
enter an argv vector, command output, or an operator-facing refusal.
"""

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gideon.host.sysio import PathLike

LDAP_PASSWORD: Final = Path("/etc/gideon/secrets/ldap_bind_password")
LDAP_CA: Final = Path("/etc/gideon/ca.pem")
# ldapsearch exits with the LDAP result code; noSuchObject (RFC 4511 §4.1.9)
# is what a base-scope search at an absent DN returns.
NO_SUCH_OBJECT: Final = 32
# Exempt operational constant: one directory query's bound, so a hung
# controller cannot stall preflight or the nightly reconcile.
LDAP_TIMEOUT_SECONDS: Final = 60.0


@dataclass(frozen=True, slots=True)
class Entry:
    """One LDIF entry with a distinguished name and normalized attributes."""

    dn: str
    attributes: dict[str, tuple[str, ...]]


_FILTER_ESCAPES: Final = (("\\", "\\5c"), ("*", "\\2a"), ("(", "\\28"), (")", "\\29"), ("\x00", "\\00"))


def filter_value(value: str) -> str:
    """Escape *value* for use as an assertion value inside an LDAP filter (RFC 4515).

    A distinguished name is the common case: ``CN=Last\\, First`` carries a
    backslash that would otherwise be read as filter syntax.
    """

    for character, escaped in _FILTER_ESCAPES:
        value = value.replace(character, escaped)
    return value


def bind_identity(bind_user: str, domain: str) -> str:
    """Return the UPN/DN used for an AD simple bind."""

    if "@" in bind_user or "=" in bind_user:
        return bind_user
    return f"{bind_user}@{domain}"


def ldapsearch_argv(
    host: str,
    port: int,
    bind_user: str,
    base: str,
    filter_expression: str,
    attributes: tuple[str, ...] = (),
    *,
    scope: str = "sub",
    password_path: PathLike = LDAP_PASSWORD,
) -> list[str]:
    """Build one password-free ``ldapsearch`` invocation over LDAPS."""

    return [
        "env",
        f"LDAPTLS_CACERT={LDAP_CA}",
        "ldapsearch",
        "-x",
        "-H",
        f"ldaps://{host}:{port}",
        "-D",
        bind_identity(bind_user, host),
        "-y",
        str(password_path),
        "-b",
        base,
        "-s",
        scope,
        "-LLL",
        filter_expression,
        *attributes,
    ]


def _unfold_ldif(lines: list[str]) -> list[str]:
    unfolded: list[str] = []
    for line in lines:
        if line.startswith(" ") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _ldif_value(value: str) -> str:
    if value.startswith(":"):
        return base64.b64decode(value[1:].lstrip()).decode("utf-8")
    return value.lstrip()


def read_ldif(text: str) -> tuple[Entry, ...]:
    """Read ordinary and folded LDIF records into immutable attribute tuples."""

    entries: list[Entry] = []
    record: list[str] = []

    def finish() -> None:
        if not record:
            return
        dn: str | None = None
        attributes: dict[str, list[str]] = {}
        for line in _unfold_ldif(record):
            if not line or line.startswith("#") or ":" not in line:
                continue
            name, _separator, value = line.partition(":")
            if not name:
                continue
            normalized = name.casefold()
            decoded = _ldif_value(value)
            if normalized == "dn":
                dn = decoded
            else:
                attributes.setdefault(normalized, []).append(decoded)
        if dn is not None:
            entries.append(
                Entry(dn=dn, attributes={
                    name: tuple(values) for name, values in attributes.items()
                })
            )

    for line in text.splitlines():
        if not line:
            finish()
            record = []
        elif line.lower() != "version: 1" or record:
            record.append(line)
    finish()
    return tuple(entries)
