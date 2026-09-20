"""Tripwire from slice-1 ticket 54: this module names no office value and
derives its public-suffix set from the allowlist.
"""

from __future__ import annotations

import io
import ipaddress
import re
import tempfile
import tokenize
import unittest
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from gideon.host.egress import load_egress_allowlist
from gideon.host.images import LIBVIRT_BRIDGE_CIDR
from gideon.host.site import FIELD_REGISTRY
from tools.exportboundary import absent_from_export
from tools.pinwatch.skills import load_skills_lock

ROOT = Path(__file__).resolve().parent.parent
_SKILLS_LOCK = ROOT / "skills-lock.json"
_DEFAULT_EGRESS = ROOT / "config" / "egress.yaml"
_SKIP_NAMES = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
# The registry marker is the one home for the tripwire's site-key vocabulary.
_SITE_KEYS = frozenset(
    spec.path.rsplit(".", 1)[-1]
    for spec in FIELD_REGISTRY
    if spec.office_value
)
_PRIVATE_SUFFIXES = frozenset(
    {"local", "lan", "corp", "home", "intranet", "private", "pvt", "ad"}
)
_RESERVED_LABELS = frozenset({"example", "test", "invalid", "localhost", "internal"})
# A workflow's expression over the Actions event context is dotted syntax, never
# a hostname; the exemption holds under .github/workflows/ alone (slice-1 ticket 56).
_ACTIONS_EVENT_CONTEXT = "github.event."
# Registrable domains the tree cites beyond the egress allowlist's hosts: the
# documentation hosts of the research notes, the ADRs, the licences, the
# workflows, and the runbooks. A host the product or its tooling reaches lives
# in config/egress.yaml and is seeded from there, never restated here; a name
# invented for a test or an example is a reserved documentation name, never an
# entry. The public-suffix set is derived from these entries and the seed.
_CURATED_ALLOWLIST: tuple[str, ...] = (
    "age-encryption.org",
    "archives.gov",
    "bing.com",
    "box.com",
    "brave.com",
    "buildkite.com",
    "caddyserver.com",
    "chatgpt.com",
    "claude.ai",
    "containerd.io",
    "cornell.edu",
    "free.law",
    "freedesktop.org",
    "gcr.io",
    "github.blog",
    "google.com",
    "googleapis.com",
    "googlesource.com",
    "grafana.com",
    "grafana.org",
    "gstatic.com",
    "healthchecks.io",
    "json-schema.org",
    "microsoft.com",
    "openai.com",
    "openrouter.ai",
    "openssh.com",
    "openwebui.com",
    "pkg.dev",
    "playwright.dev",
    "prometheus.io",
    "pypa.io",
    "pypi.org",
    "python.org",
    "quay.io",
    "readthedocs.io",
    "redhat.com",
    "searxng.org",
    "socket.io",
    "whatwg.org",
    "yaml.org",
)
# The export tree (ticket 56's boundary) holds about five hundred files; a glob
# mistake that walked one directory would fall under this floor.
MINIMUM_SCANNED_FILES: int = 400


def _ipv4(octets: tuple[int, int, int, int], prefix: int | None = None) -> str:
    value = ".".join(str(octet) for octet in octets)
    return f"{value}/{prefix}" if prefix is not None else value


def _network(octets: tuple[int, int, int, int], prefix: int) -> ipaddress.IPv4Network:
    return cast(ipaddress.IPv4Network, ipaddress.ip_network(_ipv4(octets, prefix)))


_RFC1918_NETWORKS = (
    _network((10, 0, 0, 0), 8),
    _network((172, 16, 0, 0), 12),
    _network((192, 168, 0, 0), 16),
)
_RFC1918_RANGES = frozenset(str(network) for network in _RFC1918_NETWORKS)
_LIBVIRT_NETWORK = ipaddress.ip_network(LIBVIRT_BRIDGE_CIDR)
_DOCKER_NETWORKS = tuple(_network((172, second, 0, 0), 16) for second in range(17, 32))
_IPV4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:/[0-9]{1,2})?(?![0-9])")
_DOTTED_TOKEN = re.compile(r"[A-Za-z0-9_]+(?:[A-Za-z0-9_-]*[A-Za-z0-9_])?(?:\.[A-Za-z0-9_]+(?:[A-Za-z0-9_-]*[A-Za-z0-9_])?)+")
_DN = re.compile(
    r"(?i)\b(?:dc\s*=\s*[A-Za-z0-9-]+)(?:\s*,\s*dc\s*=\s*[A-Za-z0-9-]+)+"
)
_HTTP_ACCESS_LOG = re.compile(r'"[A-Z]+ [^"]+ HTTP/[0-9]\.[0-9]"')
_PLACEHOLDER = re.compile(
    r"<(?:[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*(?:\[[0-9]+\])?"
    r"|address in lan_cidrs\[[0-9]+\]|redacted)>"
)
_SSH_TARGET = re.compile(r"(?:^|\s)ssh(?:\s+-\S+(?:\s+\S+)?)*\s+$", re.IGNORECASE)
# The token kinds that carry a Python source's text: plain strings, comments,
# and — since the tokenizer split f-strings (Python 3.12) and t-strings (3.14)
# into parts — the middle parts of those, which hold the literal text.
_LITERAL_TOKEN_TYPES: frozenset[int] = frozenset(
    kind
    for kind in (
        tokenize.STRING,
        tokenize.COMMENT,
        getattr(tokenize, "FSTRING_MIDDLE", None),
        getattr(tokenize, "TSTRING_MIDDLE", None),
    )
    if kind is not None
)


@dataclass(frozen=True, slots=True)
class _Finding:
    path: Path
    line: int
    what: str
    fix: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.what}. Fix: {self.fix}"


def _skill_directories(root: Path) -> frozenset[Path]:
    lock_path = root / _SKILLS_LOCK.name
    if not lock_path.is_file():
        if absent_from_export(_SKILLS_LOCK.name, ROOT):
            return frozenset()
        lock_path = _SKILLS_LOCK
    lock = load_skills_lock(lock_path.read_text(encoding="utf-8"))
    return frozenset(root / ".claude" / "skills" / entry.name for entry in lock.entries)


def _walk(root: Path) -> Iterator[Path]:
    skill_directories = _skill_directories(root)
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in _SKIP_NAMES for part in relative_parts):
            continue
        if any(skill in path.parents for skill in skill_directories):
            continue
        if ".claude" in relative_parts:
            marker = relative_parts.index(".claude")
            if len(relative_parts) > marker + 1 and relative_parts[marker + 1] == "worktrees":
                continue
            # A skill's Codex run logs under .claude/skills/<name>/state/ are gitignored.
            if (
                len(relative_parts) > marker + 4
                and relative_parts[marker + 1] == "skills"
                and relative_parts[marker + 3] == "state"
            ):
                continue
        yield path


def _registrable(host: str) -> str:
    parsed = ipaddress.ip_address(host) if _is_ip(host) else None
    if parsed is not None:
        return host
    labels = host.lower().rstrip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _egress_allowlist(root: Path) -> tuple[frozenset[str], tuple[str, ...]]:
    egress_path = root / "config" / "egress.yaml"
    if not egress_path.is_file():
        egress_path = _DEFAULT_EGRESS
    result = load_egress_allowlist(egress_path)
    if not result.ok or result.allowlist is None:
        raise AssertionError(result.errors)
    allowlist = set(_CURATED_ALLOWLIST)
    hosts: list[str] = []
    for group in result.allowlist.groups:
        for item in group.hosts:
            hosts.append(item.host)
            allowlist.add(_registrable(item.host))
    return frozenset(allowlist), tuple(hosts)


def _public_suffixes(allowlist: Iterable[str]) -> frozenset[str]:
    return frozenset(entry.rsplit(".", 1)[-1].lower() for entry in allowlist if "." in entry)


def _is_reserved(labels: list[str]) -> bool:
    lowered = [label.lower() for label in labels]
    return (
        "example" in lowered
        or any(label in _RESERVED_LABELS for label in lowered[-1:])
        or tuple(lowered[-2:]) == ("home", "arpa")
    )


def _is_hostname_position(line: str, start: int, *, forced: bool) -> bool:
    if forced:
        return True
    before = line[:start]
    if before.endswith("://"):
        return True
    if before.endswith("@"):
        if len(before) <= 1 or not (before[-2].isalnum() or before[-2] in "_-"):
            return False
        candidate = _DOTTED_TOKEN.match(line[start:])
        labels = candidate.group(0).split(".") if candidate is not None else []
        return not (
            len(labels) == 2
            and re.fullmatch(r"[0-9]+(?:-[0-9]+)*", labels[0])
            and labels[1].lower() in {"zip", "json"}
        )
    return _SSH_TARGET.search(before) is not None


def _in_placeholder(text: str, start: int) -> bool:
    return any(match.start() <= start < match.end() for match in _PLACEHOLDER.finditer(text))


def _candidate_boundary(text: str, start: int) -> bool:
    if start and (text[start - 1].isalnum() or text[start - 1] in "_-."):
        return False
    return not (
        start
        and text[start - 1] == "/"
        and text[max(0, start - 3) : start] != "://"
    )


def _docker_access_log(line: str, value: str) -> bool:
    return re.search(rf"{re.escape(value)}:[0-9]+\b", line) is not None and _HTTP_ACCESS_LOG.search(line) is not None


def _range_definition_allowed(
    path: Path, line: str, *, markdown_prose: bool
) -> bool:
    return (
        path.suffix.lower() in {".md", ".markdown"}
        and markdown_prose
        and re.search(r"\blan_cidrs\s*:", line, re.IGNORECASE) is None
    )


def _address_findings(
    path: Path,
    line_number: int,
    line: str,
    *,
    markdown_prose: bool,
) -> list[_Finding]:
    findings: list[_Finding] = []
    for match in _IPV4.finditer(line):
        value = match.group(0)
        address_text, _, prefix_text = value.partition("/")
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            continue
        network = next(
            (private for private in _RFC1918_NETWORKS if address in private), None
        )
        if network is None:
            continue
        if value in _RFC1918_RANGES and _range_definition_allowed(
            path, line, markdown_prose=markdown_prose
        ):
            continue
        if address in _LIBVIRT_NETWORK:
            continue
        if any(address in docker for docker in _DOCKER_NETWORKS) and _docker_access_log(
            line, address_text
        ):
            continue
        suffix = f"/{prefix_text}" if prefix_text else ""
        findings.append(
            _Finding(
                path,
                line_number,
                f"RFC 1918 address {address_text}{suffix}",
                "replace it with an RFC 5737 network or the site key's placeholder",
            )
        )
    return findings


def _hostname_finding(
    path: Path,
    line_number: int,
    token: str,
    *,
    hostname_position: bool,
    allowlist: frozenset[str],
    public_suffixes: frozenset[str],
) -> _Finding | None:
    labels = token.rstrip(".").lower().split(".")
    if len(labels) < 2 or all(label.isdigit() for label in labels):
        return None
    final = labels[-1]
    reserved = _is_reserved(labels)
    allowed = any(token.lower().rstrip(".") == item or token.lower().endswith("." + item) for item in allowlist)
    if reserved:
        return None
    if final in _PRIVATE_SUFFIXES:
        return _Finding(
            path,
            line_number,
            f"hostname {token} uses a private-use suffix",
            "replace it with a documentation value — RFC 2606 — or the site key's placeholder",
        )
    if allowed:
        return None
    if final in public_suffixes:
        return _Finding(
            path,
            line_number,
            f"hostname {token} uses an unallowlisted public domain",
            "replace it with a documentation value, or add its registrable domain to the allowlist in this test",
        )
    if hostname_position:
        return _Finding(
            path,
            line_number,
            f"hostname {token} appears in a hostname position",
            "replace it with a documentation value or the site key's placeholder",
        )
    return None


def _actions_expression(path: Path, token: str) -> bool:
    return token.startswith(_ACTIONS_EVENT_CONTEXT) and path.parts[-3:-1] == (
        ".github",
        "workflows",
    )


def _hostname_findings(
    path: Path,
    line_number: int,
    line: str,
    *,
    forced: bool,
    allowlist: frozenset[str],
    public_suffixes: frozenset[str],
) -> list[_Finding]:
    findings: list[_Finding] = []
    for match in _DOTTED_TOKEN.finditer(line):
        token = match.group(0)
        if _actions_expression(path, token):
            continue
        if _in_placeholder(line, match.start()):
            continue
        if not _candidate_boundary(line, match.start()):
            continue
        if len(token.split(".")) == 4 and all(part.isdigit() for part in token.split(".")):
            continue
        finding = _hostname_finding(
            path,
            line_number,
            token,
            hostname_position=_is_hostname_position(line, match.start(), forced=forced),
            allowlist=allowlist,
            public_suffixes=public_suffixes,
        )
        if finding is not None:
            findings.append(finding)
    return findings


def _dn_findings(path: Path, line_number: int, line: str) -> list[_Finding]:
    findings: list[_Finding] = []
    for match in _DN.finditer(line):
        labels = [part.split("=", 1)[1].strip() for part in re.split(r"\s*,\s*", match.group(0))]
        if _is_reserved(labels):
            continue
        findings.append(
            _Finding(
                path,
                line_number,
                f"distinguished name {match.group(0)} is outside a documentation domain",
                "replace it with a documentation domain's chain or <auth.ldap.search_base>",
            )
        )
    return findings


def _yaml_site_key(line: str) -> bool:
    content = line.split("#", 1)[0].strip()
    if ":" not in content:
        return False
    key = content.split(":", 1)[0].strip()
    return key in _SITE_KEYS


def _markdown_prose(lines: list[str], path: Path) -> list[bool]:
    if path.suffix.lower() not in {".md", ".markdown"}:
        return [False] * len(lines)
    fenced = False
    prose: list[bool] = []
    for line in lines:
        marker = line.lstrip().startswith(("```", "~~~"))
        if marker:
            prose.append(False)
            fenced = not fenced
        else:
            prose.append(not fenced)
    return prose


def _text_findings(
    path: Path,
    text: str,
    *,
    allowlist: frozenset[str],
    public_suffixes: frozenset[str],
) -> list[_Finding]:
    findings: list[_Finding] = []
    if path.suffix.lower() == ".py":
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            for token in tokens:
                if token.type not in _LITERAL_TOKEN_TYPES:
                    continue
                for offset, fragment in enumerate(token.string.splitlines() or [token.string]):
                    line_number = token.start[0] + offset
                    findings.extend(
                        _address_findings(path, line_number, fragment, markdown_prose=False)
                    )
                    findings.extend(
                        _hostname_findings(
                            path,
                            line_number,
                            fragment,
                            forced=False,
                            allowlist=allowlist,
                            public_suffixes=public_suffixes,
                        )
                    )
                    findings.extend(_dn_findings(path, line_number, fragment))
        except (IndentationError, tokenize.TokenError):
            pass
        return findings

    lines = text.splitlines()
    prose = _markdown_prose(lines, path)
    for line_number, line in enumerate(lines, start=1):
        findings.extend(
            _address_findings(path, line_number, line, markdown_prose=prose[line_number - 1])
        )
        findings.extend(
            _hostname_findings(
                path,
                line_number,
                line,
                forced=_yaml_site_key(line) if path.suffix.lower() in {".yaml", ".yml"} else False,
                allowlist=allowlist,
                public_suffixes=public_suffixes,
            )
        )
        findings.extend(_dn_findings(path, line_number, line))
    return findings


def _twin_finding(root: Path) -> _Finding | None:
    config = root / "config" / "site.example.yaml"
    twin = root / ".scratch" / "greenfield-spec" / "assets" / "23-site-example.yaml"
    if not config.is_file() or not twin.is_file() or config.read_bytes() == twin.read_bytes():
        return None
    return _Finding(
        twin,
        1,
        "example site twin differs from the maintained config",
        "write config/site.example.yaml over the twin",
    )


def findings(root: Path) -> list[str]:
    """Return one rendered office-value finding for each violating line."""

    allowlist, _ = _egress_allowlist(root)
    public_suffixes = _public_suffixes(allowlist)
    rendered: list[_Finding] = []
    for path in _walk(root):
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        rendered.extend(
            _text_findings(
                path,
                text,
                allowlist=allowlist,
                public_suffixes=public_suffixes,
            )
        )
    twin = _twin_finding(root)
    if twin is not None:
        rendered.append(twin)
    return [finding.render() for finding in rendered]


def _seed_text(*parts: str) -> str:
    return ".".join(parts)


def _seed_range(octets: tuple[int, int, int, int], prefix: int) -> str:
    return _ipv4(octets, prefix)


def _seed_dn(labels: tuple[str, ...]) -> str:
    return ",".join(f"DC={label}" for label in labels)


class OfficeValueTripwireTests(unittest.TestCase):
    def test_real_tree_is_clean(self) -> None:
        self.assertEqual(findings(ROOT), [])

    def test_walk_has_a_file_count_floor(self) -> None:
        self.assertGreaterEqual(len(tuple(_walk(ROOT))), MINIMUM_SCANNED_FILES)

    def test_seeded_rules_and_exemptions(self) -> None:
        private_host = _seed_text("service", "corp")
        public_host = _seed_text("service", "unlisted", "com")
        unusual_host = _seed_text("service", "unlisted", "xyz")
        documentation_host = _seed_text("service", "example", "com")
        safe_dn = _seed_dn(("ad", "test"))
        bad_dn = _seed_dn(("unlisted", "com"))
        private_address = _seed_range((10, 0, 0, 9), 32)
        docker_address = str(next(_DOCKER_NETWORKS[1].hosts()))
        docker_plain = str(next(_DOCKER_NETWORKS[2].hosts()))
        docker_port = str(next(_DOCKER_NETWORKS[3].hosts()))
        libvirt_address = str(next(_LIBVIRT_NETWORK.hosts()))
        range_text = "\n".join(sorted(_RFC1918_RANGES))
        allowlisted_host = _egress_allowlist(ROOT)[1][0]
        actions_token = _seed_text("github", "event", "repository", "private")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / ".scratch" / "greenfield-spec" / "assets").mkdir(parents=True)
            (root / "config" / "site.example.yaml").write_text("same\n", encoding="utf-8")
            (root / ".scratch" / "greenfield-spec" / "assets" / "23-site-example.yaml").write_text(
                "different\n", encoding="utf-8"
            )
            (root / "bad.md").write_text(
                f"private: {private_host}\n"
                f"public: {public_host}\n"
                f"url: https://{unusual_host}/api\n"
                f"autolink: <https://{unusual_host}/api>\n"
                f"address: {private_address}\n"
                f"dn: {bad_dn}\n"
                f"safe-dn: {safe_dn}\n"
                f"safe: {documentation_host}\n"
                f"safe-private: {_seed_text('service', 'internal')} {_seed_text('service', 'home', 'arpa')} {_seed_text('service', 'test')}\n"
                f"ssh: ssh {unusual_host}\n"
                f"placeholder: https://<auth.ldap.host>/api\n"
                "single: https://localhost/api\n"
                f"libvirt: {libvirt_address}\n"
                f'access: {docker_address}:1234 - "GET /v1/models HTTP/1.1" 200\n'
                f"plain: {docker_plain}\n"
                f"ordinary: {docker_port}:1234\n"
                f"ranges: {range_text}\n"
                "```\n"
                f"fenced: {_seed_range((10, 0, 0, 0), 8)}\n"
                "```\n"
                f"expression: {actions_token}\n",
                encoding="utf-8",
            )
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "seed.yml").write_text(
                f"    if: github.event_name == 'push' && {actions_token}\n",
                encoding="utf-8",
            )
            (root / "site.yaml").write_text(
                f"hostname: {unusual_host}\n"
                "hostname: <auth.ldap.host>\n"
                f"lan_cidrs: [{_seed_range((10, 0, 0, 0), 8)}]\n",
                encoding="utf-8",
            )
            (root / "source.py").write_text(
                f"attribute.{private_host}.value\n"
                f"value = {private_host!r}\n"
                f"# {private_host}\n"
                'path = f"https://' + private_host + '/{value}"\n',
                encoding="utf-8",
            )
            (root / "allowlisted.txt").write_text(
                f"https://{allowlisted_host}/\n",
                encoding="utf-8",
            )
            (root / ".claude" / "skills" / "x" / "state").mkdir(parents=True)
            (root / ".claude" / "skills" / "x" / "state" / "run.log").write_text(
                f"private: {private_host}\n",
                encoding="utf-8",
            )
            (root / ".claude" / "skills" / "x" / "SKILL.md").write_text(
                f"private: {private_host}\n",
                encoding="utf-8",
            )
            seeded = findings(root)
        located = {
            (Path(match.group(1)).name, int(match.group(2)))
            for match in (re.match(r"^(.*):([0-9]+): ", line) for line in seeded)
            if match is not None
        }
        text = "\n".join(seeded)
        self.assertTrue(all(". Fix: " in line for line in seeded), text)
        # bad.md, line by line: the private suffix, the unlisted public domain, the
        # URL authority, the autolink, the address, the DN, the ssh target, the bare
        # Docker address, the ported one on an ordinary line, and the fenced range
        # are reported; the reserved names, the placeholder, the single label, the
        # libvirt address, the access-log address, and the prose definitions are not.
        reported = {("bad.md", number) for number in (1, 2, 3, 4, 5, 6, 10, 15, 16, 21, 23)}
        silent = {("bad.md", number) for number in (7, 8, 9, 11, 12, 13, 14, 17, 18, 19)}
        self.assertTrue(reported <= located, text)
        self.assertTrue(located.isdisjoint(silent), text)
        # A hostname-kind site key is judged whatever its suffix, a placeholder never,
        # and a range definition in a YAML value is a finding.
        self.assertIn(("site.yaml", 1), located)
        self.assertNotIn(("site.yaml", 2), located)
        self.assertIn(("site.yaml", 3), located)
        # A Python source is read through its literals and comments alone — a
        # plain string, a comment, and an f-string's text, never an attribute chain.
        self.assertNotIn(("source.py", 1), located)
        self.assertIn(("source.py", 2), located)
        self.assertIn(("source.py", 3), located)
        self.assertIn(("source.py", 4), located)
        self.assertIn(("23-site-example.yaml", 1), located)
        self.assertFalse(any(name == "allowlisted.txt" for name, _ in located), text)
        # The Actions event context is an expression in a workflow and a hostname anywhere else.
        self.assertFalse(any(name == "seed.yml" for name, _ in located), text)
        # A skill's Codex state is skipped; the skill's other files are scanned.
        self.assertNotIn(("run.log", 1), located)
        self.assertIn(("SKILL.md", 1), located)
        self.assertIn("private-use suffix", text)
        self.assertIn("unallowlisted public domain", text)
        self.assertIn(f"hostname {unusual_host} appears in a hostname position", text)
        self.assertIn("RFC 1918 address", text)
        self.assertIn("distinguished name", text)
        self.assertIn("example site twin differs", text)


if __name__ == "__main__":
    unittest.main()
