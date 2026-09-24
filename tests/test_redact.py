"""Contracts for slice-1 ticket 55's capture-time redaction."""

import ast
import io
import sys
import unittest
from pathlib import Path
from typing import ClassVar

from gideon.host.apply import PRINT_ONCE_SUFFIX
from gideon.host.site import (
    FIELD_REGISTRY,
    SiteConfig,
    group_name,
    load_site,
    parse_group_dn,
    render_errors,
)
from tools.redact import cli
from tools.redact.core import redact

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests/fixtures/site/redact-office.yaml"
INVALID_FIXTURE = ROOT / "tests/fixtures/site/unknown-key.yaml"


def _value(config: SiteConfig, path: str) -> object:
    current: object = config
    for segment in path.split("."):
        current = getattr(current, "from_" if segment == "from" else segment)
    return current


class RedactContracts(unittest.TestCase):
    """Exercise the registry-derived vocabulary and the command's byte seam."""

    site: ClassVar[SiteConfig]

    @classmethod
    def setUpClass(cls) -> None:
        result = load_site(FIXTURE)
        if result.config is None:
            raise AssertionError(render_errors(result.errors))
        cls.site = result.config

    def test_marked_registry_is_exactly_the_sixteen_office_value_paths(self) -> None:
        expected = {
            "hostname",
            "lan_cidrs",
            "auth.ldap.host",
            "auth.ldap.search_base",
            "auth.ldap.bind_user",
            "auth.ldap.users_group",
            "auth.ldap.admins_group",
            "auth.ldap.mirror_groups",
            "backup.target.host",
            "backup.target.user",
            "alerts.smtp.host",
            "alerts.smtp.from",
            "alerts.smtp.user",
            "alerts.recipients",
            "registry",
            "egress_proxy",
        }
        marked = {spec.path for spec in FIELD_REGISTRY if spec.office_value}
        self.assertEqual(marked, expected)
        for spec in FIELD_REGISTRY:
            if spec.office_value:
                with self.subTest(path=spec.path):
                    self.assertIn(
                        spec.kind,
                        {
                            "string",
                            "non-empty string",
                            "string list",
                            "non-empty string list",
                            "CIDR list",
                        },
                    )

    def test_each_marked_value_is_replaced_from_the_loaded_fixture(self) -> None:
        for spec in FIELD_REGISTRY:
            if not spec.office_value:
                continue
            value = _value(self.site, spec.path)
            if spec.default is not None and value == spec.default:
                continue
            values = value if isinstance(value, list) else [value]
            for index, item in enumerate(values):
                if not isinstance(item, str) or not item:
                    continue
                placeholder = (
                    f"<{spec.path}{f'[{index}]' if isinstance(value, list) else ''}>"
                )
                with self.subTest(path=spec.path, index=index):
                    self.assertIn(placeholder, redact(item, self.site))

    def test_transcript_shapes_defaults_and_placeholders(self) -> None:
        site = self.site
        docker_address = ".".join(str(octet) for octet in (172, 17, 0, 1))
        decoded_group = group_name(site.auth.ldap.users_group)
        escaped_group = next(
            component
            for attribute, component in parse_group_dn(site.auth.ldap.users_group)
            if attribute == "cn"
        )
        transcript = (
            "\n".join(
                (
                    f"url https://{site.hostname}/health",
                    f"upn alice@{site.auth.ldap.host}",
                    f"subdomain dc1.{site.auth.ldap.host}",
                    f"certificate CN={site.auth.ldap.host.upper()}",
                    "dn cn=last\\, first,ou=security groups,dc=example,dc=org",
                    f"firewall from {site.lan_cidrs[0]}",
                    "address 192.0.2.42",
                    f"mail {site.alerts.smtp.from_} {site.alerts.recipients[1]}",
                    f"default {site.auth.ldap.admins_group}",
                    f"docker {docker_address}",
                    "loopback 127.0.0.1",
                    "already <hostname>",
                    f"base {site.auth.ldap.search_base}",
                    f"group {site.auth.ldap.users_group}",
                    f"decoded group name {decoded_group}",
                    f"escaped group name {escaped_group}",
                    f"mirror {site.auth.ldap.mirror_groups[0]}",
                )
            )
            + "\n"
        )
        redacted = redact(transcript, site)
        expected = (
            "\n".join(
                (
                    "url https://<hostname>/health",
                    "upn alice@<auth.ldap.host>",
                    "subdomain dc1.<auth.ldap.host>",
                    "certificate CN=<auth.ldap.host>",
                    "dn <auth.ldap.users_group>",
                    "firewall from <lan_cidrs[0]>",
                    "address <address in lan_cidrs[0]>",
                    "mail <alerts.smtp.from> <alerts.recipients[1]>",
                    f"default {site.auth.ldap.admins_group}",
                    f"docker {docker_address}",
                    "loopback 127.0.0.1",
                    "already <hostname>",
                    "base <auth.ldap.search_base>",
                    "group <auth.ldap.users_group>",
                    "decoded group name <auth.ldap.users_group>",
                    "escaped group name <auth.ldap.users_group>",
                    "mirror <auth.ldap.mirror_groups[0]>",
                )
            )
            + "\n"
        )
        self.assertEqual(redacted, expected)

    def test_non_ascii_value_secret_shapes_and_idempotence(self) -> None:
        site = self.site
        text = (
            f"recipient {site.alerts.recipients[1]}\n"
            "gideon_admin_password (break-glass administrator): secret"
            f"{PRINT_ONCE_SUFFIX}\n"
            "older_password (break-glass administrator): older-secret — into the office password manager now (§1.7).\n"
            "age identity (store it in the office password manager now): "
            "AGE-SECRET-KEY-1abc\n"
            "short age key: AGE-SECRET-KEY-1abc\n"
        )
        redacted = redact(text, site)
        self.assertIn("recipient <alerts.recipients[1]>", redacted)
        self.assertIn(f"<redacted>{PRINT_ONCE_SUFFIX}", redacted)
        self.assertIn(
            "older_password (break-glass administrator): <redacted> — into the office "
            "password manager now (§1.7).",
            redacted,
        )
        self.assertIn(
            "age identity (store it in the office password manager now): <redacted>",
            redacted,
        )
        self.assertIn("short age key: AGE-SECRET-KEY-1<redacted>", redacted)
        self.assertEqual(redact(redacted, site), redacted)

    def test_command_redacts_fixture_and_round_trips_undecodable_bytes(self) -> None:
        source = io.BytesIO(b"https://gideon.example.org\n")
        output = io.BytesIO()
        errors = io.StringIO()
        code = cli.main(
            ["--site", str(FIXTURE)],
            stdin=source,
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), b"https://<hostname>\n")
        self.assertEqual(errors.getvalue(), "")

        undecodable = io.BytesIO(b"unchanged \xff\n")
        output = io.BytesIO()
        self.assertEqual(
            cli.main(
                ["--site", str(FIXTURE)],
                stdin=undecodable,
                stdout=output,
                stderr=io.StringIO(),
            ),
            0,
        )
        self.assertEqual(output.getvalue(), b"unchanged \xff\n")

    def test_command_refusal_uses_loader_errors_and_writes_no_stdout(self) -> None:
        output = io.BytesIO()
        errors = io.StringIO()
        loaded = load_site(INVALID_FIXTURE)
        code = cli.main(
            ["--site", str(INVALID_FIXTURE)],
            stdin=io.BytesIO(b"ignored\n"),
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), b"")
        self.assertEqual(errors.getvalue(), render_errors(loaded.errors) + "\n")

    def test_redact_modules_keep_the_bare_host_import_boundary(self) -> None:
        """The command runs with the box's system Python: the standard library,
        ``yaml``, ``gideon.host``, its own package, and the ownership hand-back."""

        stdlib = frozenset(sys.stdlib_module_names) | {"yaml"}
        own = ("tools.redact", "tools.ownership", "gideon.host")

        def permitted(target: str) -> bool:
            if target.partition(".")[0] in stdlib:
                return True
            return any(target == name or target.startswith(name + ".") for name in own)

        for path in sorted((ROOT / "tools/redact").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    targets = [node.module or ""]
                else:
                    continue
                for target in targets:
                    self.assertTrue(permitted(target), f"{path}:{node.lineno} imports {target}")


if __name__ == "__main__":
    unittest.main()
