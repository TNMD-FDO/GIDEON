"""Contracts for the committed egress allowlist and its loader (spec §2.3)."""

import unittest
from pathlib import Path

from gideon.host.egress import (
    default_egress_path,
    load_egress_allowlist,
    render_errors,
    validate_egress_allowlist,
)

ARTIFACT = Path("config/egress.yaml")


class CommittedArtifact(unittest.TestCase):
    def test_committed_artifact_loads_with_all_groups(self) -> None:
        result = load_egress_allowlist(ARTIFACT)

        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.allowlist is not None
        self.assertEqual(result.allowlist.version, 7)
        self.assertEqual(
            [group.name for group in result.allowlist.groups],
            ["host-provisioning", "install-upgrade", "corpus", "image-build"],
        )

    def test_install_upgrade_group_pins_live_hf_hosts(self) -> None:
        """cdn-lfs.huggingface.co is retired (NXDOMAIN, verified 2026-09-01)."""

        result = load_egress_allowlist(ARTIFACT)
        assert result.allowlist is not None
        group = result.allowlist.group("install-upgrade")
        assert group is not None
        entries = {entry.host: entry for entry in group.hosts}
        self.assertNotIn("cdn-lfs.huggingface.co", entries)
        for host in ("cdn-lfs.hf.co", "cdn-lfs-us-1.hf.co", "cas-bridge.xethub.hf.co"):
            self.assertIn(host, entries)
            self.assertEqual(entries[host].expect, (403,))
        self.assertEqual(entries["ghcr.io"].probe_url, "https://ghcr.io/v2/")
        self.assertEqual(entries["ghcr.io"].expect, ())
        self.assertEqual(
            entries["pkg-containers.githubusercontent.com"].expect,
            (400,),
        )

    def test_host_provisioning_group_has_ticket_02_additions(self) -> None:
        """Ticket 02 spec bug (a): the registry image host and the VM image host."""

        result = load_egress_allowlist(ARTIFACT)
        assert result.allowlist is not None
        group = result.allowlist.group("host-provisioning")
        assert group is not None
        hosts = {entry.host for entry in group.hosts}
        self.assertIn("registry-1.docker.io", hosts)
        self.assertIn("auth.docker.io", hosts)
        self.assertIn("production.cloudfront.docker.com", hosts)
        self.assertIn("cloud-images.ubuntu.com", hosts)
        self.assertIn("objects.githubusercontent.com", hosts)

    def test_image_build_group_holds_the_build_hosts(self) -> None:
        """Ticket 16: the hosts apt reaches inside a GIDEON image build (§2.4)."""

        result = load_egress_allowlist(ARTIFACT)
        assert result.allowlist is not None
        group = result.allowlist.group("image-build")
        assert group is not None
        self.assertEqual(
            [entry.host for entry in group.hosts],
            ["apt.postgresql.org", "deb.debian.org"],
        )
        for entry in group.hosts:
            self.assertTrue(entry.probe_url.startswith("https://"))

    def test_default_path_points_at_the_committed_artifact(self) -> None:
        self.assertEqual(default_egress_path().resolve(), ARTIFACT.resolve())


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": 1,
        "groups": {
            "host-provisioning": [{"host": "a.example", "probe_url": "https://a.example/"}],
            "install-upgrade": [{"host": "b.example", "probe_url": "https://b.example/"}],
            "corpus": [{"host": "c.example", "probe_url": "https://c.example/"}],
            "image-build": [{"host": "d.example", "probe_url": "https://d.example/"}],
        },
    }
    document.update(overrides)
    return document


class Validation(unittest.TestCase):
    def assert_refuses(self, document: dict[str, object], fragment: str) -> None:
        errors = validate_egress_allowlist(document)
        rendered = render_errors(errors)
        self.assertTrue(errors, "expected a refusal")
        self.assertIn(fragment, rendered)
        for line in rendered.splitlines():
            self.assertIn("Fix:", line)

    def test_valid_document_has_no_errors(self) -> None:
        self.assertEqual(validate_egress_allowlist(_document()), [])

    def test_missing_version_refuses(self) -> None:
        document = _document()
        del document["version"]
        self.assert_refuses(document, "version")

    def test_non_positive_version_refuses(self) -> None:
        self.assert_refuses(_document(version=0), "version")

    def test_unknown_root_key_refuses(self) -> None:
        self.assert_refuses(_document(verison=1), "verison")

    def test_unknown_group_refuses(self) -> None:
        document = _document()
        groups = document["groups"]
        assert isinstance(groups, dict)
        groups["staging"] = []
        self.assert_refuses(document, "staging")

    def test_missing_group_refuses(self) -> None:
        document = _document()
        groups = document["groups"]
        assert isinstance(groups, dict)
        del groups["corpus"]
        self.assert_refuses(document, "corpus")

    def test_duplicate_host_within_group_refuses(self) -> None:
        document = _document()
        groups = document["groups"]
        assert isinstance(groups, dict)
        groups["corpus"] = [
            {"host": "c.example", "probe_url": "https://c.example/"},
            {"host": "c.example", "probe_url": "https://c.example/again"},
        ]
        self.assert_refuses(document, "unique")

    def test_non_url_probe_refuses(self) -> None:
        document = _document()
        groups = document["groups"]
        assert isinstance(groups, dict)
        groups["corpus"] = [{"host": "c.example", "probe_url": "ftp://c.example/"}]
        self.assert_refuses(document, "probe_url")

    def test_expect_must_be_a_non_empty_status_list(self) -> None:
        document = _document()
        groups = document["groups"]
        assert isinstance(groups, dict)
        for bad in ([], [200, True], ["403"], [42], 403):
            groups["corpus"] = [
                {"host": "c.example", "probe_url": "https://c.example/", "expect": bad}
            ]
            with self.subTest(bad=bad):
                self.assert_refuses(document, "expect")

    def test_valid_expect_list_is_accepted(self) -> None:
        document = _document()
        groups = document["groups"]
        assert isinstance(groups, dict)
        groups["corpus"] = [
            {"host": "c.example", "probe_url": "https://c.example/", "expect": [403]}
        ]
        self.assertEqual(validate_egress_allowlist(document), [])

    def test_entry_unknown_key_refuses(self) -> None:
        document = _document()
        groups = document["groups"]
        assert isinstance(groups, dict)
        groups["corpus"] = [
            {"host": "c.example", "probe_url": "https://c.example/", "port": 443}
        ]
        self.assert_refuses(document, "port")


class Loading(unittest.TestCase):
    def test_missing_file_refuses_with_fix(self) -> None:
        result = load_egress_allowlist("/nonexistent/egress.yaml")
        self.assertFalse(result.ok)
        self.assertIn("missing", render_errors(result.errors))
        self.assertIn("Fix:", render_errors(result.errors))
