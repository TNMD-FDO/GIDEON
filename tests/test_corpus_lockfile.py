"""Behavior of corpus lockfile loading, rendering, and cross-artifact checks."""

import hashlib
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from gideon.host.corpus.lockfile import (
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
from gideon.host.corpus.sources import SourceDefinition
from gideon.host.courts import default_courts_path, load_court_map
from gideon.host.egress import default_egress_path, load_egress_allowlist
from gideon.host.render.worker import PIPELINE_VERSION
from gideon.host.report import Problem

FIXTURES = Path(__file__).parent / "fixtures/corpus"
VALID = FIXTURES / "corpus-2099-01-03.yaml"
DERIVED = FIXTURES / "corpus-2099-01-07.yaml"
CAPTURED = FIXTURES / "corpus-2099-01-09.yaml"
KNOWN = {
    "example": SourceDefinition(
        "example", "https://example.invalid/archive/", True, ("ca6",),
        lambda: (), lambda _: Problem("Fictitious source has no index reader", "Use the fixture."),
    ),
    "saved-example": SourceDefinition(
        "saved-example", "https://capture.example.invalid/table", False, (),
        lambda: (), lambda _: Problem("Fictitious source has no index reader", "Use the fixture."),
        captured_file="saved.html", joined="2099-01-09",
    ),
}


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

    def test_derived_fixture_round_trips_and_copies_companions(self) -> None:
        base = load_lockfile(FIXTURES / "corpus-2099-01-06.yaml", known_sources=KNOWN)
        derived = load_lockfile(DERIVED, known_sources=KNOWN)
        self.assertTrue(base.ok, render_errors(base.errors))
        self.assertTrue(derived.ok, render_errors(derived.errors))
        assert base.lockfile is not None and derived.lockfile is not None
        self.assertEqual(base.lockfile.pipeline, PIPELINE_VERSION)
        self.assertEqual(derived.lockfile.pipeline, PIPELINE_VERSION)
        self.assertEqual(derived.lockfile.base, base.lockfile.label)
        self.assertEqual(render_lockfile(derived.lockfile), DERIVED.read_text())
        for name, pin in derived.lockfile.sources.items():
            base_pin = base.lockfile.sources[name]
            self.assertEqual(pin.entries, base_pin.entries)
            self.assertEqual(pin.index, base_pin.index)
            for filename in (f"{name}.sha256", *(f"{name}.{item.name}" for item in pin.index)):
                self.assertEqual((DERIVED.with_suffix("") / filename).read_bytes(),
                                 (FIXTURES / base.lockfile.label / filename).read_bytes())
            self.assertTrue(set(base_pin.courts or ()) < set(pin.courts or ()))

    def test_broken_variants_refuse_their_own_fault(self) -> None:
        cases = {
            "corpus-2099-01-04": "duplicate mapping key",
            "corpus-2099-01-05": "sidecar digest differs",
            "corpus-2099-01-08": "index document digest differs",
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
        self.edit("pipeline: 1.0.0", "pipeline: next")
        self.refusal("label", "cut_at", "pipeline")

    def test_base_label_and_date(self) -> None:
        self.edit("reason: tranche\n", "reason: tranche\nbase: corpus-yesterday\n")
        self.refusal("base", "expected a lockfile label")
        self.edit("base: corpus-yesterday", "base: corpus-2099-02-30")
        self.refusal("base", "expected a valid YYYY-MM-DD date")

    def test_unknown_source_and_missing_known_source(self) -> None:
        self.edit("  example:", "  alien:")
        self.refusal("unknown source", "missing required source")

    def test_courts_are_sorted_unique_and_source_specific(self) -> None:
        self.edit("    - ca6", "    - ca6\n    - ca6")
        self.refusal("sorted and unique")
        self.assertIn("does not carry courts", render_errors(
            load_lockfile(self.path, known_sources={
                **KNOWN, "example": replace(KNOWN["example"], carries_courts=False),
            }).errors
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


class CapturedPin(unittest.TestCase):
    """The saved document and the snapshot sidecar pin the same bytes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / CAPTURED.name
        shutil.copy2(CAPTURED, self.path)
        shutil.copytree(CAPTURED.with_suffix(""), self.path.with_suffix(""))
        self.document = yaml.safe_load(self.path.read_text())
        self.raw = self.document["sources"]["saved-example"]

    def refusal(self, *details: str) -> None:
        self.path.write_text(yaml.safe_dump(self.document, sort_keys=False))
        result = load_lockfile(self.path, known_sources=KNOWN)
        self.assertFalse(result.ok)
        rendered = render_errors(result.errors)
        for detail in details:
            self.assertIn(detail, rendered)
        self.assertTrue(all("Restore the lockfile" in error.fix for error in result.errors))

    def test_round_trip_and_capture_status_from_definition(self) -> None:
        result = load_lockfile(self.path, known_sources=KNOWN)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.lockfile is not None
        self.assertEqual(render_lockfile(result.lockfile), CAPTURED.read_text())
        self.assertFalse(result.lockfile.sources["example"].captured)
        pin = result.lockfile.sources["saved-example"]
        self.assertTrue(pin.captured)
        entry, index = pin.entries[0], pin.index[0]
        self.assertEqual((entry.path, entry.sha256, entry.size),
                         (index.name, index.sha256, index.size))
        self.assertEqual(index.url, pin.base_url)
        self.assertEqual(render_sidecar(pin.entries).encode(),
                         (self.path.with_suffix("") / "saved-example.sha256").read_bytes())
        self.assertNotIn("captured:", render_lockfile(result.lockfile))

    def test_mirror_refuses(self) -> None:
        self.raw["mirror_url"] = "https://mirror.example.invalid/"
        self.refusal("mirror_url", "captured source must have no mirror URL")

    def test_two_files_refuse_even_when_totals_and_digest_agree(self) -> None:
        folder = self.path.with_suffix("")
        sidecar = folder / "saved-example.sha256"
        sidecar.write_text(sidecar.read_text() + "z-other.html  " + "e" * 64 + "  7\n")
        self.raw["sidecar_sha256"] = hashlib.sha256(sidecar.read_bytes()).hexdigest()
        self.raw["files"] += 1
        self.raw["bytes"] += 7
        self.refusal("files", "captured source must pin exactly one file")

    def test_no_index_document_refuses(self) -> None:
        self.raw["index"] = []
        self.refusal("index", "captured source must carry exactly one index document")

    def test_two_index_documents_refuse(self) -> None:
        index = dict(self.raw["index"][0], name="other.html")
        folder = self.path.with_suffix("")
        shutil.copy2(folder / "saved-example.saved.html", folder / "saved-example.other.html")
        self.raw["index"].append(index)
        self.refusal("index", "captured source must carry exactly one index document")

    def test_document_name_disagrees_with_sidecar(self) -> None:
        folder = self.path.with_suffix("")
        (folder / "saved-example.saved.html").rename(folder / "saved-example.other.html")
        self.raw["index"][0]["name"] = "other.html"
        self.refusal("index[0].name", "captured document name differs from sidecar path")

    def test_document_digest_and_size_disagree_with_sidecar(self) -> None:
        folder = self.path.with_suffix("")
        data = b"A different fictitious saved page.\n"
        (folder / "saved-example.saved.html").write_bytes(data)
        self.raw["index"][0].update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        self.refusal("index[0].sha256", "captured document digest differs from sidecar",
                     "index[0].size", "captured document size differs from sidecar")

    def test_document_url_disagrees_with_provenance(self) -> None:
        self.raw["index"][0]["url"] = "https://capture.example.invalid/other"
        self.refusal("index[0].url", "captured document URL differs from base_url")

    def test_url_disagreement_collected_with_missing_sidecar(self) -> None:
        (self.path.with_suffix("") / "saved-example.sha256").unlink()
        self.raw["index"][0]["url"] = "https://capture.example.invalid/other"
        self.refusal("saved-example.sha256", "index[0].url",
                     "captured document URL differs from base_url")

    def test_companion_bytes_still_checked(self) -> None:
        (self.path.with_suffix("") / "saved-example.saved.html").write_bytes(b"damaged")
        self.refusal("index document digest differs", "index document size differs")


class JoinedSources(unittest.TestCase):
    """Only definitions that have joined by the label date are carried."""

    def test_older_fixture_omits_later_source(self) -> None:
        result = load_lockfile(VALID, known_sources=KNOWN)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.lockfile is not None
        self.assertEqual(tuple(result.lockfile.sources), ("example",))

    def test_source_present_before_joined_date_refuses(self) -> None:
        later = {**KNOWN, "saved-example": replace(KNOWN["saved-example"], joined="2099-01-10")}
        result = load_lockfile(CAPTURED, known_sources=later)
        self.assertFalse(result.ok)
        self.assertIn("source joined on 2099-01-10", render_errors(result.errors))

    def test_source_required_at_or_after_joined_date(self) -> None:
        for joined in ("2099-01-03", "2099-01-02"):
            with self.subTest(joined=joined):
                known = {**KNOWN, "saved-example": replace(KNOWN["saved-example"], joined=joined)}
                result = load_lockfile(VALID, known_sources=known)
                self.assertFalse(result.ok)
                self.assertIn("sources.saved-example", render_errors(result.errors))
                self.assertIn("missing required source", render_errors(result.errors))

    def test_order_follows_filtered_registry(self) -> None:
        later = replace(KNOWN["saved-example"], name="later-example", joined="2099-01-10")
        known = {"later-example": later, **KNOWN}
        result = load_lockfile(CAPTURED, known_sources=known)
        self.assertTrue(result.ok, render_errors(result.errors))
        result = load_lockfile(CAPTURED, known_sources={
            "saved-example": KNOWN["saved-example"], "example": KNOWN["example"],
        })
        self.assertFalse(result.ok)
        self.assertIn("sources must follow registry order", render_errors(result.errors))


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

    def test_captured_provenance_host_skipped_and_fetched_host_checked(self) -> None:
        loaded = load_lockfile(CAPTURED, known_sources=KNOWN)
        assert loaded.lockfile is not None
        result = load_egress_allowlist(default_egress_path())
        assert result.allowlist is not None
        errors = check_egress_hosts(loaded.lockfile, result.allowlist)
        self.assertEqual(tuple(error.key_path for error in errors), ("sources.example.base_url",))
        captured = replace(loaded.lockfile, sources={
            "saved-example": loaded.lockfile.sources["saved-example"],
        })
        self.assertEqual(check_egress_hosts(captured, result.allowlist), ())

    def test_state_comparison_ignores_header_and_index_digest(self) -> None:
        pin = self.lockfile.sources["example"]
        changed_index = replace(pin.index[0], sha256="f" * 64)
        other = replace(self.lockfile, label="corpus-2099-01-04", reason="quarterly",
                        cut_at="2099-01-04T00:00:00Z", sources={
                            "example": replace(pin, index=(changed_index,))
                        })
        self.assertTrue(same_state(self.lockfile, other))
        self.assertFalse(same_state(self.lockfile, replace(other, pipeline="9.9.9")))
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

    def test_derived_base_must_be_loaded_and_earlier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label in ("corpus-2099-01-06", DERIVED.stem):
                shutil.copy2(FIXTURES / f"{label}.yaml", root)
                shutil.copytree(FIXTURES / label, root / label)
            self.assertEqual(read_lockfile_directory(root, known_sources=KNOWN).errors, ())
            path = root / DERIVED.name
            original = path.read_text()
            for base, detail in (
                ("corpus-2099-01-05", "is not a lockfile in this directory"),
                (DERIVED.stem, "is not earlier than this lockfile"),
            ):
                with self.subTest(base=base):
                    path.write_text(original.replace("base: corpus-2099-01-06", f"base: {base}"))
                    result = read_lockfile_directory(root, known_sources=KNOWN)
                    self.assertEqual(len(result.errors), 1)
                    self.assertEqual(result.errors[0].key_path, str(path))
                    self.assertIn(f"base {base} {detail}", result.errors[0].problem)
                    self.assertTrue(result.errors[0].fix)
