"""Contract tests for the site-file loader and validator."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast

import yaml  # type: ignore[import-untyped]

from gideon.host.site import (
    _EXAMPLE_LINES,
    derive_search_base,
    load_site,
    load_site_text,
    read_site_file,
    render_errors,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config/site.example.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures/site"
GENERAL_FIX = "Fix: Edit /etc/gideon/site.yaml; consult config/site.example.yaml."

REQUIRED_PATHS = {
    "office.name",
    "office.short_name",
    "office.timezone",
    "hostname",
    "lan_cidrs",
    "jurisdiction.circuit",
    "jurisdiction.districts",
    "jurisdiction.states",
    "auth.ldap.host",
    "backup.target.host",
    "backup.target.path",
    "alerts.smtp.host",
    "alerts.smtp.from",
    "alerts.recipients",
}

DEFAULTS = {
    "auth.ldap.port": 636,
    "auth.ldap.search_base": "DC=example,DC=org",
    "auth.ldap.bind_user": "svc-gideon-ldap",
    "auth.ldap.users_group": "GIDEON-Users",
    "auth.ldap.admins_group": "GIDEON-Admins",
    "auth.ldap.mirror_groups": [],
    "auth.session_hours": 12,
    "backup.local_days": 7,
    "backup.remote_days": 30,
    "backup.drill_interval": "1m",
    "alerts.smtp.port": 25,
    "hardware_profile": "2x96v-256d",
    "registry": "ghcr.io/tnmd-fdo",
    "egress_proxy": "",
    "web.search": "on",
    "web.engines": ["brave", "bing", "startpage", "wikipedia"],
    "web.domain_filter": [],
    "retention.chats": "90d",
    "prompt_logging": "metadata_only",
}


def leaf_paths(value: object, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return {prefix}
    paths: set[str] = set()
    for key, child in value.items():
        child_path = f"{prefix}.{key}" if prefix else key
        paths.update(leaf_paths(child, child_path))
    return paths


def config_value(config: object, path: str) -> object:
    current = config
    for part in path.split("."):
        if part == "from":
            part = "from_"
        current = getattr(current, part)
    return current


class SiteValidation(unittest.TestCase):
    def test_load_site_text_constructs_and_collects_errors(self) -> None:
        """Slice-1 ticket 55 loads rendered text through the normal site pipeline."""

        fixture = (FIXTURES / "redact-office.yaml").read_text(encoding="utf-8")
        loaded = load_site_text(fixture)
        self.assertTrue(loaded.ok, render_errors(loaded.errors))
        self.assertIsNotNone(loaded.config)
        assert loaded.config is not None
        self.assertEqual(
            loaded.config.auth.ldap.search_base,
            derive_search_base(loaded.config.auth.ldap.host),
        )

        invalid = load_site_text("office:\n  name: [\n")
        self.assertFalse(invalid.ok)
        self.assertIn("YAML parse error", render_errors(invalid.errors))

    def test_example_loads_and_has_exact_required_leaf_set(self) -> None:
        result = load_site(EXAMPLE)
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.config)

        document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(leaf_paths(document), REQUIRED_PATHS)
        self.assertEqual(len(REQUIRED_PATHS), 14)

    def test_example_resolves_pinned_defaults(self) -> None:
        result = load_site(EXAMPLE)
        self.assertIsNotNone(result.config)
        assert result.config is not None

        self.assertEqual(len(DEFAULTS), 19)
        for path, expected in DEFAULTS.items():
            with self.subTest(path=path):
                self.assertEqual(config_value(result.config, path), expected)

    def test_unknown_key_fixture_has_nearest_hint_and_fix(self) -> None:
        rendered = self._render_fixture("unknown-key.yaml")
        self.assertEqual(
            rendered,
            "Unknown key 'office.short_nam'; nearest valid key is "
            f"'office.short_name'. {GENERAL_FIX}",
        )

    def test_missing_required_fixture_points_to_example_and_has_fix(self) -> None:
        rendered = self._render_fixture("missing-required.yaml")
        self.assertEqual(
            rendered,
            f"Missing required key 'office.name'; see "
            f"config/site.example.yaml:14. {GENERAL_FIX}",
        )

    def test_bad_enum_fixtures_list_all_allowed_values(self) -> None:
        expected = {
            "bad-drill-interval.yaml": (
                "backup.drill_interval",
                "1w, 2w, 1m, 3m, 6m, 1y",
                "bad",
            ),
            "bad-prompt-logging.yaml": (
                "prompt_logging",
                "metadata_only, full",
                "bad",
            ),
            "bad-retention.yaml": (
                "retention.chats",
                "1d, 7d, 30d, 60d, 90d, 180d, 1y",
                "bad",
            ),
            "bad-search.yaml": ("web.search", "on, off", "bad"),
        }
        for filename, (path, allowed, value) in expected.items():
            with self.subTest(filename=filename):
                rendered = self._render_fixture(filename)
                self.assertEqual(
                    rendered,
                    f"Invalid value for '{path}': expected one of: {allowed} "
                    f"(got '{value}'). {GENERAL_FIX}",
                )

    def test_shape_refusals(self) -> None:
        cases = {
            "invalid timezone": ("office.timezone", "Not/AZone"),
            "invalid CIDR": ("lan_cidrs", ["not-a-cidr"]),
            "wrong scalar type": ("hostname", 42),
            "wrong list type": ("web.engines", "duckduckgo"),
        }
        for label, (path, value) in cases.items():
            with self.subTest(case=label):
                document = self._example_document()
                self._set_path(document, path, value)
                result = self._load_document(document)
                self.assertFalse(result.ok)
                self.assertIn(path, {error.key_path for error in result.errors})

        for path in ("lan_cidrs", "jurisdiction.districts", "alerts.recipients"):
            with self.subTest(empty_list=path):
                document = self._example_document()
                self._set_path(document, path, [])
                result = self._load_document(document)
                self.assertIn(path, {error.key_path for error in result.errors})

    def test_web_engines_and_domain_filter_refusals(self) -> None:
        """§3.3's three web keys (slice-1 ticket 15): six engine names, bare domains."""

        allowed = "duckduckgo, brave, bing, startpage, wikipedia, google"
        self.assertEqual(
            self._render_fixture("bad-engines.yaml"),
            f"Invalid value for 'web.engines': expected each entry to be one of: {allowed} "
            f"(got ['duckduckgo', 'nope', 'duckduckgo']). {GENERAL_FIX}",
        )
        rendered = self._render_fixture("bad-domain-filter.yaml")
        self.assertIn("Invalid value for 'web.domain_filter': expected a bare domain name such as law.cornell.edu", rendered)
        self.assertTrue(rendered.endswith(GENERAL_FIX))

        document = self._example_document()
        self._set_path(document, "web.engines", ["duckduckgo", "duckduckgo"])
        result = self._load_document(document)
        self.assertIn("expected no repeated entry", render_errors(result.errors))

        document = self._example_document()
        self._set_path(document, "web.engines", [])
        result = self._load_document(document)
        self.assertFalse(result.ok)
        error = next(error for error in result.errors if error.key_path == "web.engines")
        self.assertIn("at least one engine while web.search is on", error.problem)
        self.assertIn("web.search: off", error.fix)
        self._set_path(document, "web.search", "off")
        self.assertTrue(self._load_document(document).ok)

        document = self._example_document()
        self._set_path(document, "web.engines", ["google", "wikipedia"])
        self._set_path(document, "web.domain_filter", ["law.cornell.edu", "uscourts.gov", "a-b.c1.example"])
        result = self._load_document(document)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.config is not None
        self.assertEqual(result.config.web.engines, ["google", "wikipedia"])
        self.assertEqual(result.config.web.domain_filter, ["law.cornell.edu", "uscourts.gov", "a-b.c1.example"])
        for bad in ("Law.Cornell.edu", "localhost", "-a.example", "a..example", "a.example/path", "127.0.0.1", "192.0.2.0/24"):
            with self.subTest(domain=bad):
                document = self._example_document()
                self._set_path(document, "web.domain_filter", [bad])
                self.assertFalse(self._load_document(document).ok)

    def test_backup_retention_days_have_a_positive_lower_bound(self) -> None:
        for path in ("backup.local_days", "backup.remote_days"):
            for value in (0, -1):
                with self.subTest(path=path, value=value):
                    document = self._example_document()
                    self._set_path(document, path, value)
                    result = self._load_document(document)
                    self.assertFalse(result.ok)
                    self.assertEqual([error.key_path for error in result.errors], [path])
                    rendered = render_errors(result.errors)
                    self.assertIn(path, rendered)
                    self.assertIn("at least 1", rendered)
                    self.assertIn(f"Set {path} to at least 1", rendered)

    def test_scalar_section_reports_mapping_error_without_missing_cascade(self) -> None:
        document = self._example_document()
        document["auth"] = "not a mapping"
        result = self._load_document(document)

        self.assertEqual([error.key_path for error in result.errors], ["auth"])
        self.assertIn("expected a mapping", render_errors(result.errors))
        self.assertNotIn("auth.ldap.host", render_errors(result.errors))

    def test_multiple_errors_are_collected_from_one_fixture(self) -> None:
        result = load_site(FIXTURES / "multiple-errors.yaml")
        paths = {error.key_path for error in result.errors}
        self.assertFalse(result.ok)
        self.assertTrue(
            {
                "office.short_nam",
                "office.short_name",
                "office.timezone",
                "lan_cidrs",
                "web.search",
            }.issubset(paths)
        )
        rendered = render_errors(result.errors)
        self.assertGreaterEqual(len(rendered.splitlines()), 5)
        for line, error in zip(rendered.splitlines(), result.errors, strict=True):
            self.assertTrue(line.endswith(error.fix))

    def test_search_base_derivation_and_string_search_enum(self) -> None:
        self.assertEqual(
            derive_search_base("example.org"), "DC=example,DC=org"
        )
        for value in ("on", "off"):
            with self.subTest(value=value):
                document = self._example_document()
                document["web"] = {"search": value}
                result = self._load_document(document)
                self.assertTrue(result.ok)
                self.assertIsNotNone(result.config)
                assert result.config is not None
                self.assertEqual(result.config.web.search, value)

        for value in ("true", "yes"):
            with self.subTest(value=value):
                contents = (
                    (FIXTURES / "unknown-key.yaml").read_text(encoding="utf-8")
                    + f"\nweb:\n  search: {value}\n"
                )
                result = self._load_text(contents)
                self.assertFalse(result.ok)
                self.assertEqual(result.errors[0].key_path, "office.short_nam")
                self.assertIn("web.search", {error.key_path for error in result.errors})

    def test_yaml_safe_loader_resolver_isolation(self) -> None:
        self.assertIs(yaml.safe_load("value: true")["value"], True)

    def test_example_line_table_points_at_each_key(self) -> None:
        lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
        for path, line_number in _EXAMPLE_LINES.items():
            with self.subTest(path=path):
                key = path.rsplit(".", 1)[-1]
                self.assertGreaterEqual(line_number, 1)
                self.assertLessEqual(line_number, len(lines))
                self.assertIn(f"{key}:", lines[line_number - 1])

    def test_loader_refusals_have_problem_specific_fixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            duplicate = root / "duplicate.yaml"
            duplicate.write_text("key: one\nkey: two\n", encoding="utf-8")
            result = read_site_file(duplicate)
            self.assertIn("duplicate mapping key", result.errors[0].problem)
            self.assertEqual(result.errors[0].fix, GENERAL_FIX.removeprefix("Fix: "))

            empty = root / "empty.yaml"
            empty.write_bytes(b"")
            result = read_site_file(empty)
            self.assertIn("site file is empty", render_errors(result.errors))
            self.assertTrue(render_errors(result.errors).endswith(GENERAL_FIX))

            non_mapping = root / "sequence.yaml"
            non_mapping.write_text("- one\n- two\n", encoding="utf-8")
            result = read_site_file(non_mapping)
            self.assertIn("root must be a mapping", render_errors(result.errors))
            self.assertTrue(render_errors(result.errors).endswith(GENERAL_FIX))

            parse_error = root / "parse.yaml"
            parse_error.write_text("office:\n  name: [\n", encoding="utf-8")
            result = read_site_file(parse_error)
            rendered = render_errors(result.errors)
            self.assertIn("YAML parse error", rendered)
            self.assertIn("line 3", rendered)
            self.assertTrue(rendered.endswith(GENERAL_FIX))

            undecodable = root / "undecodable.yaml"
            undecodable.write_bytes(b"office: \xff\n")
            result = read_site_file(undecodable)
            rendered = render_errors(result.errors)
            self.assertIn("not valid UTF-8", rendered)
            self.assertIn(f"Re-save {undecodable} as UTF-8.", rendered)

            missing = root / "missing.yaml"
            result = read_site_file(missing)
            rendered = render_errors(result.errors)
            self.assertIn(f"Create {missing} from config/site.example.yaml.", rendered)

            unreadable = root / "unreadable.yaml"
            unreadable.write_text("key: value\n", encoding="utf-8")
            if os.geteuid() == 0:
                self.skipTest("root can read chmod 000 files")
            unreadable.chmod(0)
            try:
                result = read_site_file(unreadable)
                rendered = render_errors(result.errors)
                self.assertIn("unreadable", rendered)
                self.assertIn(
                    f"Correct ownership and mode so the process can read {unreadable}.",
                    rendered,
                )
            finally:
                unreadable.chmod(0o600)

    def _render_fixture(self, filename: str) -> str:
        result = load_site(FIXTURES / filename)
        self.assertFalse(result.ok)
        return render_errors(result.errors)

    @staticmethod
    def _example_document() -> dict[str, object]:
        return copy.deepcopy(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))

    def _load_document(self, document: dict[str, object]):
        return self._load_text(yaml.safe_dump(document, sort_keys=False))

    @staticmethod
    def _load_text(contents: str):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            return load_site(handle.name)

    @staticmethod
    def _set_path(document: dict[str, object], path: str, value: object) -> None:
        parts = path.split(".")
        current: dict[str, object] = document
        for part in parts[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                child = {}
                current[part] = child
            current = cast(dict[str, object], child)
        current[parts[-1]] = value
