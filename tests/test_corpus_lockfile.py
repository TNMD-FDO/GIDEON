"""Behavior of corpus lockfile loading, rendering, and cross-artifact checks."""

import hashlib
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from gideon.host.corpus.lockfile import (
    PIPELINE_VERSION,
    READABLE_SCHEMAS,
    SCHEMA_VERSION,
    check_courts,
    check_egress_hosts,
    load_lockfile,
    read_lockfile_directory,
    render_errors,
    render_lockfile,
    render_sidecar,
    same_state,
)
from gideon.host.courts import default_courts_path, load_court_map
from gideon.host.egress import default_egress_path, load_egress_allowlist

FIXTURES = Path(__file__).parent / "fixtures/corpus"
VALID = FIXTURES / "corpus-2099-01-03.yaml"
KNOWN = {"example": True}


class Fixture(unittest.TestCase):
    """The fictitious fixture is a complete, readable cut."""

    def test_load_and_render_byte_for_byte(self) -> None:
        result = load_lockfile(VALID, known_sources=KNOWN)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.lockfile is not None
        lockfile = result.lockfile
        self.assertEqual(lockfile.schema, SCHEMA_VERSION)
        self.assertIn(lockfile.schema, READABLE_SCHEMAS)
        self.assertEqual(lockfile.pipeline, PIPELINE_VERSION)
        self.assertEqual(render_lockfile(lockfile), VALID.read_text())
        for name, pin in lockfile.sources.items():
            sidecar = VALID.with_suffix("") / f"{name}.sha256"
            self.assertEqual(render_sidecar(pin.entries).encode(), sidecar.read_bytes())
            self.assertEqual(hashlib.sha256(sidecar.read_bytes()).hexdigest(), pin.sidecar_sha256)

    def test_broken_variants_refuse_their_own_fault(self) -> None:
        cases = {
            "corpus-2099-01-04": "duplicate mapping key",
            "corpus-2099-01-05": "sidecar digest differs",
            "corpus-2099-01-06": "index document digest differs",
        }
        for label, detail in cases.items():
            with self.subTest(label=label):
                result = load_lockfile(FIXTURES / f"{label}.yaml", known_sources=KNOWN)
                self.assertFalse(result.ok)
                self.assertIn(detail, render_errors(result.errors))
                self.assertTrue(all(error.fix for error in result.errors))


class Refusals(unittest.TestCase):
    """Independent errors are collected with a repair for each one."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.path = root / VALID.name
        shutil.copy2(VALID, self.path)
        shutil.copytree(VALID.with_suffix(""), self.path.with_suffix(""))

    def edit(self, before: str, after: str) -> None:
        text = self.path.read_text()
        self.assertIn(before, text)
        self.path.write_text(text.replace(before, after, 1))

    def refusal(self, *details: str) -> None:
        result = load_lockfile(self.path, known_sources=KNOWN)
        self.assertIsNone(result.lockfile)
        rendered = render_errors(result.errors)
        for detail in details:
            self.assertIn(detail, rendered)
        self.assertTrue(all("Fix:" in line for line in rendered.splitlines()))

    def test_collects_unknown_keys_and_malformed_fields(self) -> None:
        self.edit("reason: tranche", "reason: surprise\nunknown_root: true")
        self.edit("snapshot_date: '2099-01-02'", "snapshot_date: impossible\n    extra_field: true")
        self.edit("base_url: https://", "base_url: http://")
        self.refusal("unknown_root", "reason", "snapshot_date", "extra_field", "base_url")

    def test_unknown_nested_index_key_names_nearest(self) -> None:
        self.edit("    - name: listing.xml", "    - nane: listing.xml")
        self.refusal("nearest valid key is 'name'", "missing required key")

    def test_schema_names_upgrade(self) -> None:
        self.edit("schema: 1", "schema: 999")
        result = load_lockfile(self.path, known_sources=KNOWN)
        self.assertIn("Upgrade GIDEON", render_errors(result.errors))

    def test_label_timestamp_and_version(self) -> None:
        self.edit("label: corpus-2099-01-03", "label: corpus-2099-02-30")
        self.edit("cut_at: '2099-01-03T04:05:06Z'", "cut_at: yesterday")
        self.edit("pipeline: 0.0.0", "pipeline: next")
        self.refusal("label", "cut_at", "pipeline")

    def test_unknown_source_and_missing_known_source(self) -> None:
        self.edit("  example:", "  alien:")
        self.refusal("unknown source", "missing required source")

    def test_courts_are_sorted_unique_and_source_specific(self) -> None:
        self.edit("    - ca6", "    - ca6\n    - ca6")
        self.refusal("sorted and unique")
        self.assertIn("does not carry courts", render_errors(
            load_lockfile(self.path, known_sources={"example": False}).errors
        ))

    def test_sidecar_line_grammar_and_totals(self) -> None:
        sidecar = self.path.with_suffix("") / "example.sha256"
        sidecar.write_text("../outside  " + "e" * 64 + "  7\n")
        self.refusal("invalid sidecar line", "file count differs", "byte total differs")

    def test_sidecar_duplicate_and_out_of_order_paths(self) -> None:
        sidecar = self.path.with_suffix("") / "example.sha256"
        line = sidecar.read_text()
        sidecar.write_text("z/file  " + "e" * 64 + "  1\n" + line + line)
        self.refusal("repeated path", "sorted and unique")

    def test_missing_companions_are_independent(self) -> None:
        folder = self.path.with_suffix("")
        (folder / "example.sha256").unlink()
        (folder / "example.listing.xml").unlink()
        self.refusal("example.sha256", "example.listing.xml")

    def test_index_size_is_checked(self) -> None:
        result = load_lockfile(self.path, known_sources=KNOWN)
        assert result.lockfile is not None
        size = result.lockfile.sources["example"].index[0].size
        self.edit(f"      size: {size}", f"      size: {size + 1}")
        self.refusal("index document size differs")


class RelatedArtifacts(unittest.TestCase):
    """Cross-artifact checks consume already loaded release artifacts."""

    def setUp(self) -> None:
        result = load_lockfile(VALID, known_sources=KNOWN)
        assert result.lockfile is not None
        self.lockfile = result.lockfile

    def test_court_ids_resolve_in_loaded_map(self) -> None:
        result = load_court_map(default_courts_path())
        assert result.court_map is not None
        self.assertEqual(check_courts(self.lockfile, result.court_map), ())
        pin = self.lockfile.sources["example"]
        broken = replace(self.lockfile, sources={"example": replace(pin, courts=("fictional-court",))})
        self.assertIn("fictional-court", render_errors(check_courts(broken, result.court_map)))

    def test_urls_use_loaded_corpus_group(self) -> None:
        result = load_egress_allowlist(default_egress_path())
        assert result.allowlist is not None
        allowlist = result.allowlist
        self.assertIn("example.invalid", render_errors(check_egress_hosts(self.lockfile, allowlist)))
        group = allowlist.group("corpus")
        assert group is not None
        host = group.hosts[0].host
        pin = self.lockfile.sources["example"]
        allowed = replace(self.lockfile, sources={"example": replace(
            pin, base_url=f"https://{host}/archive/", mirror_url=f"https://{host}/mirror/"
        )})
        self.assertEqual(check_egress_hosts(allowed, allowlist), ())

    def test_state_comparison_ignores_header_and_index_digest(self) -> None:
        pin = self.lockfile.sources["example"]
        changed_index = replace(pin.index[0], sha256="f" * 64)
        other = replace(self.lockfile, label="corpus-2099-01-04", reason="quarterly",
                        cut_at="2099-01-04T00:00:00Z", sources={
                            "example": replace(pin, index=(changed_index,))
                        })
        self.assertTrue(same_state(self.lockfile, other))
        self.assertFalse(same_state(self.lockfile, replace(other, pipeline="1.0.0")))
        self.assertFalse(same_state(self.lockfile, replace(other, sources={
            "example": replace(pin, mirror_url="https://example.invalid/mirror/")
        })))


class Directory(unittest.TestCase):
    """The reader sees complete YAML files and ignores unfinished directories."""

    def test_empty_and_unfinished(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(read_lockfile_directory(root, known_sources=KNOWN).newest)
            (root / "corpus-2099-01-03").mkdir()
            self.assertEqual(read_lockfile_directory(root, known_sources=KNOWN).lockfiles, ())

    def test_newest_label_and_collected_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label in ("corpus-2099-01-03", "corpus-2099-01-05"):
                shutil.copy2(FIXTURES / f"{label}.yaml", root)
                shutil.copytree(FIXTURES / label, root / label)
            result = read_lockfile_directory(root, known_sources=KNOWN)
            self.assertEqual(result.newest.label if result.newest else None, VALID.stem)
            self.assertIn("sidecar digest differs", render_errors(result.errors))
