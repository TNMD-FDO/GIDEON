"""The site-file model: registry, bare-host YAML loader, typed config (§3.3–3.4).

Every leaf of ``/etc/gideon/site.yaml`` is declared once in ``FIELD_REGISTRY``;
the validator, the dataclasses, and the schema emitter all read that one table
so allowed values are written exactly once (ticket [23] item 8).
"""

import difflib
import ipaddress
import re
import zoneinfo
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, assert_never, cast

import yaml  # type: ignore[import-untyped]

from gideon.host.sysio import Host, RealHost

Kind = Literal[
    "string",
    "non-empty string",
    "int",
    "enum",
    "string list",
    "non-empty string list",
    "CIDR list",
    "timezone",
]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """The registry entry for one leaf in the site file.

    ``office_value`` marks a leaf that names a machine, network, directory
    object, mailbox, or account of the office. Evidence redaction replaces its
    value with the leaf's site-key placeholder.
    """

    path: str
    required: bool
    kind: Kind
    description: str
    default: object | None = None
    allowed_values: tuple[str, ...] = ()
    pattern: str | None = None
    derived: bool = False
    minimum: int | None = None
    office_value: bool = False


def _field(
    path: str,
    *,
    kind: Kind,
    description: str,
    required: bool = False,
    default: object | None = None,
    allowed_values: tuple[str, ...] = (),
    pattern: str | None = None,
    derived: bool = False,
    minimum: int | None = None,
    office_value: bool = False,
) -> FieldSpec:
    return FieldSpec(
        path=path,
        required=required,
        kind=kind,
        description=description,
        default=default,
        allowed_values=allowed_values,
        pattern=pattern,
        derived=derived,
        minimum=minimum,
        office_value=office_value,
    )


_RETENTION_VALUES: Final = ("1d", "7d", "30d", "60d", "90d", "180d", "1y")
_DRILL_INTERVAL_VALUES: Final = ("1w", "2w", "1m", "3m", "6m", "1y")
_PROMPT_LOGGING_VALUES: Final = ("metadata_only", "full")
_WEB_SEARCH_VALUES: Final = ("on", "off")
# The engines an office may name (§3.3's defaults: brave, bing, startpage, and
# wikipedia; DuckDuckGo and Google are allowed but off by default because
# DuckDuckGo's bot detection answers a SearXNG instance's request shape with a
# CAPTCHA at every search; ticket 71): each is the ``name:`` of a default
# engine in the pinned SearXNG image (docs/research/searxng-service-and-owui-search.md
# §5a); ``brave`` is the keyless engine, never ``braveapi``.  A release extends
# the list.
WEB_ENGINE_VALUES: Final[tuple[str, ...]] = (
    "duckduckgo",
    "brave",
    "bing",
    "startpage",
    "wikipedia",
    "google",
)
# Bare lowercase domain names only: the frontend reads a leading ``!`` as a
# block rule and an address as a network, while this key is an allow-list; a
# bare name matches itself and its subdomains on label boundaries
# (docs/research/searxng-service-and-owui-search.md §7).  The last label
# carries a letter, so a dotted address never passes as a name.
DOMAIN_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?=[a-z0-9-]*[a-z])[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"
)
GROUP_PATHS: Final = {
    "auth.ldap.users_group",
    "auth.ldap.admins_group",
    "auth.ldap.mirror_groups",
}


# Appendix C's complete leaf registry.  Defaults are kept here and are also
# used by the dataclass defaults below; search_base is the one derived default.
FIELD_REGISTRY: Final[tuple[FieldSpec, ...]] = (
    _field(
        "office.name",
        kind="non-empty string",
        required=True,
        description="General preset text and release notes.",
    ),
    _field(
        "office.short_name",
        kind="non-empty string",
        required=True,
        description="Alert subjects and the Grafana title.",
    ),
    _field(
        "office.timezone",
        kind="timezone",
        required=True,
        description="IANA timezone used by the host, containers, and schedules.",
    ),
    _field(
        "hostname",
        kind="non-empty string",
        required=True,
        description="Caddy, WEBUI_URL, and certificate subject.",
        office_value=True,
    ),
    _field(
        "lan_cidrs",
        kind="CIDR list",
        required=True,
        description="CIDRs allowed to reach HTTPS and SSH.",
        office_value=True,
    ),
    _field(
        "jurisdiction.circuit",
        kind="non-empty string",
        required=True,
        description="The office's CourtListener circuit identifier.",
    ),
    _field(
        "jurisdiction.districts",
        kind="non-empty string list",
        required=True,
        description="District identifiers used for ranking and labels.",
    ),
    _field(
        "jurisdiction.states",
        kind="string list",
        required=True,
        description="State identifiers used to expand appellate courts.",
    ),
    _field(
        "auth.ldap.host",
        kind="non-empty string",
        required=True,
        description="The AD domain name resolved to the office's domain controllers.",
        office_value=True,
    ),
    _field(
        "auth.ldap.port",
        kind="int",
        default=636,
        description="The LDAP service port.",
    ),
    _field(
        "auth.ldap.search_base",
        kind="non-empty string",
        derived=True,
        description="LDAP search base, derived from auth.ldap.host when omitted.",
        office_value=True,
    ),
    _field(
        "auth.ldap.bind_user",
        kind="non-empty string",
        default="svc-gideon-ldap",
        description="The LDAP service account name; its password is a secret file.",
        office_value=True,
    ),
    _field(
        "auth.ldap.users_group",
        kind="non-empty string",
        default="GIDEON-Users",
        description=(
            "Group CN, or its full DN when the group is outside AD's default Users container; "
            "direct membership permits login."
        ),
        office_value=True,
    ),
    _field(
        "auth.ldap.admins_group",
        kind="non-empty string",
        default="GIDEON-Admins",
        description=(
            "Group CN, or its full DN when the group is outside AD's default Users container; "
            "direct membership grants administrator status."
        ),
        office_value=True,
    ),
    _field(
        "auth.ldap.mirror_groups",
        kind="string list",
        default=[],
        description=(
            "Group CNs, or their full DNs when groups are outside AD's default Users container; "
            "direct memberships are mirrored as sharing targets."
        ),
        office_value=True,
    ),
    _field(
        "auth.session_hours",
        kind="int",
        default=12,
        description="The session lifetime in hours.",
    ),
    _field(
        "backup.target.host",
        kind="non-empty string",
        required=True,
        description="SSH host receiving dated backup snapshots.",
        office_value=True,
    ),
    _field(
        "backup.target.path",
        kind="non-empty string",
        required=True,
        description="The single target path under which dated snapshots live.",
    ),
    _field(
        "backup.target.user",
        kind="non-empty string",
        default="gideon-backup",
        description="SSH account name on the backup target.",
        office_value=True,
    ),
    _field(
        "backup.local_days",
        kind="int",
        default=7,
        minimum=1,
        description="Number of nightly backup sets retained on the server.",
    ),
    _field(
        "backup.remote_days",
        kind="int",
        default=30,
        minimum=1,
        description="Number of dated backup snapshots retained on the target.",
    ),
    _field(
        "backup.drill_interval",
        kind="enum",
        default="1m",
        allowed_values=_DRILL_INTERVAL_VALUES,
        description="Restore-drill cadence.",
    ),
    _field(
        "alerts.smtp.host",
        kind="non-empty string",
        required=True,
        description="The office SMTP relay host.",
        office_value=True,
    ),
    _field(
        "alerts.smtp.from",
        kind="non-empty string",
        required=True,
        description="The sender address for GIDEON alerts.",
        office_value=True,
    ),
    _field(
        "alerts.smtp.user",
        kind="string",
        default="",
        description="SMTP relay AUTH identity; used with secrets/smtp_password.",
        office_value=True,
    ),
    _field(
        "alerts.smtp.port",
        kind="int",
        default=25,
        description="The SMTP service port.",
    ),
    _field(
        "alerts.recipients",
        kind="non-empty string list",
        required=True,
        description="Recipients for page-class alerts.",
        office_value=True,
    ),
    _field(
        "hardware_profile",
        kind="non-empty string",
        default="2x96v-256d",
        description="The hardware profile name from models.lock.",
    ),
    _field(
        "registry",
        kind="non-empty string",
        default="ghcr.io/tnmd-fdo",
        description="Image-reference prefix for images in images.lock.",
        office_value=True,
    ),
    _field(
        "egress_proxy",
        kind="string",
        default="",
        description="Optional proxy applied to package, image, and web egress.",
        office_value=True,
    ),
    _field(
        "web.search",
        kind="enum",
        default="on",
        allowed_values=_WEB_SEARCH_VALUES,
        description="Whether web search is enabled through SearXNG.",
    ),
    _field(
        "web.engines",
        kind="string list",
        allowed_values=WEB_ENGINE_VALUES,
        default=["brave", "bing", "startpage", "wikipedia"],
        description="Search engines made available to SearXNG.",
    ),
    _field(
        "web.domain_filter",
        kind="string list",
        default=[],
        pattern=DOMAIN_NAME.pattern,
        description="Allow-list of domains for web-search results.",
    ),
    _field(
        "retention.chats",
        kind="enum",
        default="90d",
        allowed_values=_RETENTION_VALUES,
        description="Chat retention duration from last activity.",
    ),
    _field(
        "prompt_logging",
        kind="enum",
        default="metadata_only",
        allowed_values=_PROMPT_LOGGING_VALUES,
        description="Whether prompt logging stores metadata only or full prompts.",
    ),
)


def _spec(path: str) -> FieldSpec:
    for spec in FIELD_REGISTRY:
        if spec.path == path:
            return spec
    raise KeyError(path)


def _default(path: str) -> object:
    spec = _spec(path)
    if spec.required or spec.derived or spec.default is None:
        raise ValueError(f"{path} does not have a literal default")
    return spec.default


def _default_int(path: str) -> int:
    return cast(int, _default(path))


def _default_string(path: str) -> str:
    return cast(str, _default(path))


def _default_strings(path: str) -> list[str]:
    return list(cast(Iterable[str], _default(path)))


def _strings(value: object) -> list[str]:
    return list(cast(Iterable[str], value))


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value)


@dataclass(frozen=True, slots=True)
class Office:
    name: str
    short_name: str
    timezone: str


@dataclass(frozen=True, slots=True)
class Ldap:
    host: str
    port: int = field(default_factory=lambda: _default_int("auth.ldap.port"))
    search_base: str = field(default_factory=str)
    bind_user: str = field(
        default_factory=lambda: _default_string("auth.ldap.bind_user")
    )
    users_group: str = field(
        default_factory=lambda: _default_string("auth.ldap.users_group")
    )
    admins_group: str = field(
        default_factory=lambda: _default_string("auth.ldap.admins_group")
    )
    mirror_groups: list[str] = field(
        default_factory=lambda: _default_strings("auth.ldap.mirror_groups")
    )

    def __post_init__(self) -> None:
        if not self.search_base:
            object.__setattr__(self, "search_base", derive_search_base(self.host))

    @property
    def users_group_dn(self) -> str:
        """Return the users group's DN, deriving the AD default-container form."""

        return group_dn(self.users_group, self.search_base)

    @property
    def admins_group_dn(self) -> str:
        """Return the administrators group's DN, deriving the default-container form."""

        return group_dn(self.admins_group, self.search_base)

    @property
    def mirror_group_dns(self) -> tuple[str, ...]:
        """Return the configured mirror-group DNs in site-file order."""

        return tuple(group_dn(group, self.search_base) for group in self.mirror_groups)


@dataclass(frozen=True, slots=True)
class Auth:
    ldap: Ldap
    session_hours: int = field(
        default_factory=lambda: _default_int("auth.session_hours")
    )


@dataclass(frozen=True, slots=True)
class Jurisdiction:
    circuit: str
    districts: list[str]
    states: list[str]


@dataclass(frozen=True, slots=True)
class BackupTarget:
    host: str
    path: str
    user: str = field(default_factory=lambda: _default_string("backup.target.user"))


@dataclass(frozen=True, slots=True)
class Backup:
    target: BackupTarget
    local_days: int = field(default_factory=lambda: _default_int("backup.local_days"))
    remote_days: int = field(default_factory=lambda: _default_int("backup.remote_days"))
    drill_interval: str = field(
        default_factory=lambda: _default_string("backup.drill_interval")
    )


@dataclass(frozen=True, slots=True)
class Smtp:
    host: str
    from_: str
    port: int = field(default_factory=lambda: _default_int("alerts.smtp.port"))
    user: str = field(default_factory=lambda: _default_string("alerts.smtp.user"))


@dataclass(frozen=True, slots=True)
class Alerts:
    smtp: Smtp
    recipients: list[str]


@dataclass(frozen=True, slots=True)
class Web:
    search: str = field(default_factory=lambda: _default_string("web.search"))
    engines: list[str] = field(
        default_factory=lambda: _default_strings("web.engines")
    )
    domain_filter: list[str] = field(
        default_factory=lambda: _default_strings("web.domain_filter")
    )


@dataclass(frozen=True, slots=True)
class Retention:
    chats: str = field(default_factory=lambda: _default_string("retention.chats"))


@dataclass(frozen=True, slots=True)
class SiteConfig:
    office: Office
    hostname: str
    lan_cidrs: list[str]
    jurisdiction: Jurisdiction
    auth: Auth
    backup: Backup
    alerts: Alerts
    hardware_profile: str = field(
        default_factory=lambda: _default_string("hardware_profile")
    )
    registry: str = field(default_factory=lambda: _default_string("registry"))
    egress_proxy: str = field(default_factory=lambda: _default_string("egress_proxy"))
    web: Web = field(default_factory=Web)
    retention: Retention = field(default_factory=Retention)
    prompt_logging: str = field(
        default_factory=lambda: _default_string("prompt_logging")
    )

    @classmethod
    def from_mapping(cls, document: Mapping[str, object]) -> "SiteConfig":
        """Construct a fully defaulted config from a parsed site mapping.

        The mapping is expected to have passed the validation walk.  Keeping
        that precondition explicit prevents this construction helper from
        becoming a second, partial validator.
        """

        office = _mapping(document["office"])
        jurisdiction = _mapping(document["jurisdiction"])
        auth = _mapping(document["auth"])
        ldap = _mapping(auth["ldap"])
        backup = _mapping(document["backup"])
        target = _mapping(backup["target"])
        alerts = _mapping(document["alerts"])
        smtp = _mapping(alerts["smtp"])
        web = _mapping(document.get("web", {}))
        retention = _mapping(document.get("retention", {}))

        ldap_kwargs: dict[str, Any] = {"host": ldap["host"]}
        for key in (
            "port",
            "search_base",
            "bind_user",
            "users_group",
            "admins_group",
        ):
            if key in ldap:
                ldap_kwargs[key] = ldap[key]
        if "mirror_groups" in ldap:
            ldap_kwargs["mirror_groups"] = _strings(ldap["mirror_groups"])

        return cls(
            office=Office(
                name=office["name"],
                short_name=office["short_name"],
                timezone=office["timezone"],
            ),
            hostname=cast(str, document["hostname"]),
            lan_cidrs=_strings(document["lan_cidrs"]),
            jurisdiction=Jurisdiction(
                circuit=jurisdiction["circuit"],
                districts=_strings(jurisdiction["districts"]),
                states=_strings(jurisdiction["states"]),
            ),
            auth=Auth(
                ldap=Ldap(**ldap_kwargs),
                session_hours=auth.get("session_hours", _default("auth.session_hours")),
            ),
            backup=Backup(
                target=BackupTarget(
                    host=target["host"],
                    path=target["path"],
                    user=target.get("user", _default("backup.target.user")),
                ),
                local_days=backup.get("local_days", _default("backup.local_days")),
                remote_days=backup.get("remote_days", _default("backup.remote_days")),
                drill_interval=backup.get(
                    "drill_interval", _default("backup.drill_interval")
                ),
            ),
            alerts=Alerts(
                smtp=Smtp(
                    host=smtp["host"],
                    from_=smtp["from"],
                    port=smtp.get("port", _default("alerts.smtp.port")),
                    user=smtp.get("user", _default("alerts.smtp.user")),
                ),
                recipients=_strings(alerts["recipients"]),
            ),
            hardware_profile=cast(
                str, document.get("hardware_profile", _default("hardware_profile"))
            ),
            registry=cast(str, document.get("registry", _default("registry"))),
            egress_proxy=cast(
                str, document.get("egress_proxy", _default("egress_proxy"))
            ),
            web=Web(
                search=web.get("search", _default("web.search")),
                engines=_strings(web.get("engines", _default("web.engines"))),
                domain_filter=_strings(
                    web.get("domain_filter", _default("web.domain_filter"))
                ),
            ),
            retention=Retention(
                chats=retention.get("chats", _default("retention.chats"))
            ),
            prompt_logging=cast(
                str, document.get("prompt_logging", _default("prompt_logging"))
            ),
        )


def derive_search_base(host: str) -> str:
    """Derive an Active Directory search base from a DNS domain name."""

    return ",".join(f"DC={label}" for label in host.split("."))


def _split_dn(value: str) -> list[str]:
    """Split a DN at unescaped commas, retaining escaped characters."""

    parts: list[str] = []
    start = 0
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ",":
            parts.append(value[start:index])
            start = index + 1
    if escaped:
        raise ValueError("a distinguished name cannot end with an escape")
    parts.append(value[start:])
    return parts


def parse_group_dn(value: str) -> tuple[tuple[str, str], ...]:
    """Parse the simple RDN sequence needed for configured AD group DNs."""

    if not value or "=" not in value:
        raise ValueError("expected a comma-separated distinguished name")
    rdns: list[tuple[str, str]] = []
    for rdn in _split_dn(value):
        escaped = False
        separators: list[int] = []
        for index, character in enumerate(rdn):
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "=":
                separators.append(index)
        if len(separators) != 1 or separators[0] < 1:
            raise ValueError("each distinguished-name component needs an attribute")
        separator = separators[0]
        attribute = rdn[:separator].strip()
        component = rdn[separator + 1 :].strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", attribute) or not component:
            raise ValueError("each distinguished-name component needs an attribute and value")
        rdns.append((attribute.casefold(), component))
    if not any(attribute == "cn" for attribute, _ in rdns):
        raise ValueError("a group distinguished name must contain a CN")
    return tuple(rdns)


def _unescape_dn_value(value: str) -> str:
    result: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            result.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        else:
            result.append(character)
    return "".join(result)


def group_dn(value: str, search_base: str) -> str:
    """Return an explicit group DN, deriving CNs in AD's default Users container."""

    if "=" in value:
        parse_group_dn(value)
        return value
    return f"CN={value},CN=Users,{search_base}"


def group_name(value: str) -> str:
    """Return a group's CN from either a CN value or its distinguished name."""

    if "=" not in value:
        return value
    for attribute, component in parse_group_dn(value):
        if attribute == "cn":
            return _unescape_dn_value(component)
    raise ValueError("a group distinguished name must contain a CN")


@dataclass(frozen=True, slots=True)
class SiteError:
    """A structured refusal from site-file loading or validation."""

    key_path: str | None
    problem: str
    fix: str


_GENERAL_FIX: Final = "Edit /etc/gideon/site.yaml; consult config/site.example.yaml."
FILE_SCOPE: Final[str | None] = None


_EXAMPLE_LINES: Final[dict[str, int]] = {
    "office.name": 14,
    "office.short_name": 15,
    "office.timezone": 16,
    "hostname": 18,
    "lan_cidrs": 19,
    "jurisdiction.circuit": 25,
    "jurisdiction.districts": 25,
    "jurisdiction.states": 25,
    "auth.ldap.host": 29,
    "backup.target.host": 40,
    "backup.target.path": 41,
    "alerts.smtp.host": 49,
    "alerts.smtp.from": 50,
    "alerts.recipients": 53,
}


def _path(prefix: str, key: object) -> str:
    rendered = str(key)
    return f"{prefix}.{rendered}" if prefix else rendered


def _registry_tree() -> dict[str, dict[str, Any]]:
    tree: dict[str, dict[str, Any]] = {}
    for spec in FIELD_REGISTRY:
        node = tree
        for segment in spec.path.split("."):
            node = node.setdefault(segment, {})
    return tree


def _nearest_valid_path(path: str) -> str:
    matches = difflib.get_close_matches(
        path, [spec.path for spec in FIELD_REGISTRY], n=1, cutoff=0.0
    )
    return matches[0] if matches else "(none)"


def _unknown_key_error(path: str) -> SiteError:
    nearest = _nearest_valid_path(path)
    return SiteError(
        key_path=path,
        problem=f"Unknown key '{path}'; nearest valid key is '{nearest}'.",
        fix=_GENERAL_FIX,
    )


def _walk_unknown_keys(
    value: object,
    node: dict[str, dict[str, Any]],
    prefix: str,
    errors: list[SiteError],
) -> None:
    if not isinstance(value, Mapping):
        return
    for key, child_value in value.items():
        child_path = _path(prefix, key)
        if not isinstance(key, str) or key not in node:
            errors.append(_unknown_key_error(child_path))
            continue
        children = node[key]
        if children:
            _walk_unknown_keys(child_value, children, child_path, errors)


def _section_prefixes() -> set[str]:
    prefixes: set[str] = set()
    for spec in FIELD_REGISTRY:
        parts = spec.path.split(".")
        for depth in range(1, len(parts)):
            prefixes.add(".".join(parts[:depth]))
    return prefixes


def _lookup(document: Mapping[str, object], path: str) -> tuple[bool, object]:
    current: object = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _missing_error(spec: FieldSpec) -> SiteError:
    line = _EXAMPLE_LINES[spec.path]
    return SiteError(
        key_path=spec.path,
        problem=(
            f"Missing required key '{spec.path}'; "
            f"see config/site.example.yaml:{line}."
        ),
        fix=_GENERAL_FIX,
    )


def _invalid_value_error(spec: FieldSpec, value: object, detail: str) -> SiteError:
    return SiteError(
        key_path=spec.path,
        problem=f"Invalid value for '{spec.path}': {detail} (got {value!r}).",
        fix=_GENERAL_FIX,
    )


def _validate_leaf(spec: FieldSpec, value: object) -> SiteError | None:
    kind = spec.kind

    if kind == "string":
        if isinstance(value, str):
            return None
        return _invalid_value_error(spec, value, "expected a string")

    if kind == "non-empty string":
        if isinstance(value, str) and value:
            if spec.path in GROUP_PATHS and "=" in value:
                try:
                    parse_group_dn(value)
                except ValueError as exc:
                    return _invalid_value_error(
                        spec, value, f"expected a group CN or valid distinguished name ({exc})"
                    )
            return None
        return _invalid_value_error(spec, value, "expected a non-empty string")

    if kind == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            if spec.minimum is not None and value < spec.minimum:
                return SiteError(
                    key_path=spec.path,
                    problem=(
                        f"Invalid value for '{spec.path}': expected an integer "
                        f"at least {spec.minimum} (got {value!r})."
                    ),
                    fix=(
                        f"Set {spec.path} to at least {spec.minimum} in "
                        "/etc/gideon/site.yaml; consult config/site.example.yaml."
                    ),
                )
            return None
        return _invalid_value_error(spec, value, "expected an integer")

    if kind == "enum":
        if isinstance(value, str) and value in spec.allowed_values:
            return None
        allowed = ", ".join(spec.allowed_values)
        return _invalid_value_error(
            spec, value, f"expected one of: {allowed}"
        )

    if kind == "string list":
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            if spec.allowed_values:
                allowed = ", ".join(spec.allowed_values)
                if any(item not in spec.allowed_values for item in value):
                    return _invalid_value_error(
                        spec,
                        value,
                        f"expected each entry to be one of: {allowed}",
                    )
                if len(value) != len(set(value)):
                    return _invalid_value_error(
                        spec, value, "expected no repeated entry"
                    )
            if spec.pattern is not None and any(
                re.fullmatch(spec.pattern, item) is None for item in value
            ):
                return _invalid_value_error(
                    spec,
                    value,
                    "expected a bare domain name such as law.cornell.edu",
                )
            if spec.path in GROUP_PATHS:
                for item in value:
                    if "=" in item:
                        try:
                            parse_group_dn(item)
                        except ValueError as exc:
                            return _invalid_value_error(
                                spec,
                                value,
                                f"expected group CNs or valid distinguished names ({exc})",
                            )
            return None
        return _invalid_value_error(spec, value, "expected a list of strings")

    if kind == "non-empty string list":
        if (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item for item in value)
        ):
            return None
        return _invalid_value_error(
            spec, value, "expected a non-empty list of non-empty strings"
        )

    if kind == "CIDR list":
        if isinstance(value, list) and value:
            for item in value:
                if not isinstance(item, str):
                    return _invalid_value_error(
                        spec, value, "expected a list of CIDR strings"
                    )
                try:
                    ipaddress.ip_network(item)
                except ValueError:
                    return _invalid_value_error(
                        spec, value, f"'{item}' is not a valid CIDR"
                    )
            return None
        return _invalid_value_error(spec, value, "expected a non-empty list of CIDRs")

    if kind == "timezone":
        if isinstance(value, str) and value in zoneinfo.available_timezones():
            return None
        return _invalid_value_error(spec, value, "expected a valid IANA timezone")

    assert_never(kind)


def validate_site(document: Mapping[str, object]) -> list[SiteError]:
    """Return every registry and shape error found in a parsed document."""

    errors: list[SiteError] = []
    _walk_unknown_keys(document, _registry_tree(), "", errors)

    # A section holding a scalar would otherwise cascade into misleading
    # "missing required key" errors for every leaf beneath it.
    bad_sections: list[str] = []
    for prefix in sorted(_section_prefixes()):
        present, value = _lookup(document, prefix)
        if present and not isinstance(value, Mapping):
            bad_sections.append(prefix)
            errors.append(
                SiteError(
                    key_path=prefix,
                    problem=(
                        f"Invalid value for '{prefix}': expected a mapping "
                        f"of its keys (got {value!r})."
                    ),
                    fix=_GENERAL_FIX,
                )
            )

    for spec in FIELD_REGISTRY:
        if any(spec.path.startswith(bad + ".") for bad in bad_sections):
            continue
        present, value = _lookup(document, spec.path)
        if not present:
            if spec.required:
                errors.append(_missing_error(spec))
            continue
        error = _validate_leaf(spec, value)
        if error is not None:
            errors.append(error)

    search_present, search = _lookup(document, "web.search")
    if not search_present:
        search = _default("web.search")
    engines_present, engines = _lookup(document, "web.engines")
    if not engines_present:
        engines = _default("web.engines")
    if search == "on" and isinstance(engines, list) and not engines:
        errors.append(
            SiteError(
                key_path="web.engines",
                problem="web.engines must name at least one engine while web.search is on.",
                fix=(
                    "Name at least one engine in web.engines or set web.search: off "
                    "in /etc/gideon/site.yaml; consult config/site.example.yaml."
                ),
            )
        )
    return errors


def render_errors(errors: Iterable[SiteError]) -> str:
    """Render structured refusals as one printable, fix-ending line each."""

    lines = []
    for error in errors:
        problem = " ".join(error.problem.splitlines())
        lines.append(f"{problem} Fix: {error.fix}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SiteLoadResult:
    """The parsed document, constructed config, or structured errors."""

    config: SiteConfig | None = None
    document: Mapping[str, object] | None = None
    errors: tuple[SiteError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class _DuplicateKeyError(yaml.YAMLError):
    """Raised by :class:`SiteLoader` when a mapping repeats a key."""


class SiteLoader(yaml.SafeLoader):
    """SafeLoader with duplicate-key refusal and string ``on``/``off``."""


# Copy every resolver list before filtering it.  The lists are copied too:
# mutating either level must never change yaml.SafeLoader's global table.
SiteLoader.yaml_implicit_resolvers = {
    initial: [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:bool"
    ]
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping_without_duplicates(
    loader: SiteLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise _DuplicateKeyError(f"duplicate mapping key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


SiteLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_without_duplicates,
)


def _error(problem: str, fix: str = _GENERAL_FIX) -> SiteLoadResult:
    return SiteLoadResult(errors=(SiteError(key_path=FILE_SCOPE, problem=problem, fix=fix),))


def parse_site_text(text: str) -> SiteLoadResult:
    """Parse site YAML text, returning file-level failures as errors."""

    try:
        document = yaml.load(text, Loader=SiteLoader)
    except _DuplicateKeyError as exc:
        return _error(f"site file has a {exc}")
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = getattr(mark, "line", None)
        location = f" at line {line + 1}" if isinstance(line, int) else ""
        return _error(f"site file has a YAML parse error{location}: {exc}")

    if document is None:
        return _error("site file is empty")
    if not isinstance(document, Mapping):
        return _error("site file root must be a mapping")

    return SiteLoadResult(document=cast(Mapping[str, object], document))


def _load_site_document(document: Mapping[str, object]) -> SiteLoadResult:
    """Validate a parsed site document and construct its defaulted dataclasses."""

    errors = validate_site(document)
    if errors:
        return SiteLoadResult(document=document, errors=tuple(errors))
    return SiteLoadResult(
        config=SiteConfig.from_mapping(document),
        document=document,
    )


def load_site_text(text: str) -> SiteLoadResult:
    """Parse site YAML text and construct its fully defaulted dataclasses."""

    result = parse_site_text(text)
    if result.errors or result.document is None:
        return result
    return _load_site_document(result.document)


def read_site_file(path: str | Path, *, host: Host | None = None) -> SiteLoadResult:
    """Read and parse a site file, returning file-level failures as errors."""

    site_path = Path(path)
    io = host or RealHost()
    try:
        text = io.read_text(site_path)
    except FileNotFoundError:
        return _error(
            f"site file is missing: {site_path}",
            f"Create {site_path} from config/site.example.yaml.",
        )
    except PermissionError:
        return _error(
            f"site file is unreadable due to permissions: {site_path}",
            f"Correct ownership and mode so the process can read {site_path}.",
        )
    except OSError as exc:
        return _error(
            f"site file is unreadable: {site_path} ({exc})",
            f"Correct ownership and mode so the process can read {site_path}.",
        )
    except UnicodeDecodeError:
        return _error(
            f"site file is not valid UTF-8: {site_path}",
            f"Re-save {site_path} as UTF-8.",
        )

    return parse_site_text(text)


def load_site(path: str | Path, *, host: Host | None = None) -> SiteLoadResult:
    """Load a site file and construct its fully defaulted dataclasses.

    File-level and mapping validation problems are returned as structured
    errors before dataclass construction is attempted.
    """

    result = read_site_file(path, host=host)
    if result.errors or result.document is None:
        return result
    return _load_site_document(result.document)
