"""Collection selection and deterministic generation names and input hashes."""

import hashlib
import re
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from gideon.host.caselaw import CourtGeography
from gideon.host.corpus.lockfile import load_lockfile, render_errors
from gideon.host.corpus.sources import SOURCES
from gideon.host.index.generations import (
    COLLECTIONS,
    GenerationInputs,
    generation_name,
    inputs_hash,
)
from gideon.host.render import worker

FIXTURE = Path(__file__).parent / "fixtures/corpus/corpus-2099-01-03.yaml"
SPACE_ID = "example-embedding-1024"


class Collections(unittest.TestCase):
    def test_registry_is_ordered_and_selects_sources_carrying_courts(self) -> None:
        self.assertEqual(tuple(item.name for item in COLLECTIONS), worker.COLLECTION_NAMES)
        collection = COLLECTIONS[0]
        self.assertEqual(collection.profile, "caselaw")
        source = SOURCES[0]
        self.assertTrue(collection.feeds(replace(source, carries_courts=True)))
        self.assertFalse(collection.feeds(replace(source, carries_courts=False)))
        with self.assertRaises(FrozenInstanceError):
            collection.profile = "example"  # type: ignore[misc]


class Names(unittest.TestCase):
    def test_name_matches_the_shared_grammar_and_is_safe_for_the_store(self) -> None:
        for collection in COLLECTIONS:
            with self.subTest(collection=collection.name):
                name = generation_name(collection.name, FIXTURE.stem, SPACE_ID)
                self.assertEqual(name, f"{collection.name}__{FIXTURE.stem}__{SPACE_ID}")
                self.assertIsNotNone(re.fullmatch(worker.GENERATION_PATTERN, name))
                self.assertEqual(name, name.lower())
                self.assertLess(len(name.encode()), 255)

    def test_invalid_components_are_refused_with_a_fix(self) -> None:
        collection = COLLECTIONS[0].name
        for args in (
            ("unknown", FIXTURE.stem, SPACE_ID),
            (collection.upper(), FIXTURE.stem, SPACE_ID),
            (collection, "corpus-2099-1-3", SPACE_ID),
            (collection, FIXTURE.stem + "\n", SPACE_ID),
            (collection, FIXTURE.stem, ""),
            (collection, FIXTURE.stem, "Example"),
            (collection, FIXTURE.stem, "example_space"),
            (collection, FIXTURE.stem, "example--space"),
            (collection, FIXTURE.stem, "example/space"),
            (collection, FIXTURE.stem, "example-"),
        ):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "Fix:"):
                generation_name(*args)
            self.assertIsNone(re.fullmatch(worker.GENERATION_PATTERN, "__".join(args)))

    def test_byte_limit_boundary(self) -> None:
        collection = COLLECTIONS[0].name
        prefix = f"{collection}__{FIXTURE.stem}__"
        space = "a" * (254 - len(prefix.encode()))
        self.assertEqual(len(generation_name(collection, FIXTURE.stem, space).encode()), 254)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            generation_name(collection, FIXTURE.stem, space + "a")


class Hashes(unittest.TestCase):
    def setUp(self) -> None:
        loaded = load_lockfile(FIXTURE, known_sources={"example": True})
        self.assertTrue(loaded.ok, render_errors(loaded.errors))
        assert loaded.lockfile is not None
        self.lockfile = loaded.lockfile
        self.source = replace(SOURCES[0], name="example")
        self.collection = COLLECTIONS[0].name

    def test_hash_uses_canonical_lines_in_source_registry_order(self) -> None:
        pin = self.lockfile.sources[self.source.name]
        other = replace(self.source, name="second-example")
        other_pin = replace(pin, sidecar_sha256="e" * 64, courts=("z9", "a1"))
        lockfile = replace(self.lockfile, sources={other.name: other_pin, self.source.name: pin})
        canonical = "\n".join((
            self.source.name, pin.sidecar_sha256, *sorted(pin.courts or ()),
            other.name, other_pin.sidecar_sha256, *sorted(other_pin.courts or ()),
            lockfile.pipeline, SPACE_ID,
        )) + "\n"
        digest = inputs_hash(lockfile, self.collection, SPACE_ID, sources=(self.source, other))
        self.assertEqual(digest, hashlib.sha256(canonical.encode()).hexdigest())
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotEqual(digest, inputs_hash(
            lockfile, self.collection, SPACE_ID, sources=(other, self.source),
        ))

    def test_each_identity_input_moves_the_hash(self) -> None:
        pin = self.lockfile.sources[self.source.name]
        digest = inputs_hash(self.lockfile, self.collection, SPACE_ID, sources=(self.source,))
        cases = (
            (replace(self.lockfile, sources={self.source.name: replace(pin, sidecar_sha256="b" * 64)}), SPACE_ID),
            (replace(self.lockfile, sources={self.source.name: replace(pin, courts=(*(pin.courts or ()), "z9"))}), SPACE_ID),
            (replace(self.lockfile, pipeline="1000.0.0"), SPACE_ID),
            (self.lockfile, "another-example-space"),
        )
        for lockfile, space in cases:
            with self.subTest(lockfile=lockfile, space=space):
                self.assertNotEqual(digest, inputs_hash(
                    lockfile, self.collection, space, sources=(self.source,),
                ))

    def test_labels_and_acquisition_metadata_do_not_change_identity(self) -> None:
        pin = self.lockfile.sources[self.source.name]
        same_inputs = replace(
            self.lockfile, label="corpus-2099-02-03", reason="pipeline",
            cut_at="2099-02-03T00:00:00Z", base=self.lockfile.label,
            sources={self.source.name: replace(
                pin, mirror_url="https://mirror.example.test/", index=(),
                courts=tuple(reversed(pin.courts or ())),
            )},
        )
        self.assertEqual(
            inputs_hash(self.lockfile, self.collection, SPACE_ID, sources=(self.source,)),
            inputs_hash(same_inputs, self.collection, SPACE_ID, sources=(self.source,)),
        )

    def test_unrelated_sources_are_excluded(self) -> None:
        unrelated = replace(self.source, name="unrelated-example", carries_courts=False)
        extra = replace(self.lockfile, sources={
            **self.lockfile.sources,
            unrelated.name: replace(self.lockfile.sources[self.source.name], sidecar_sha256="c" * 64),
        })
        self.assertEqual(
            inputs_hash(self.lockfile, self.collection, SPACE_ID, sources=(self.source,)),
            inputs_hash(extra, self.collection, SPACE_ID, sources=(unrelated, self.source)),
        )

    def test_default_sources_use_the_committed_registry(self) -> None:
        lockfile = replace(self.lockfile, sources={SOURCES[0].name: self.lockfile.sources[self.source.name]})
        self.assertEqual(
            inputs_hash(lockfile, self.collection, SPACE_ID),
            inputs_hash(lockfile, self.collection, SPACE_ID, sources=SOURCES),
        )

    def test_unknown_collection_refuses_with_a_fix(self) -> None:
        with self.assertRaisesRegex(ValueError, "Fix:"):
            inputs_hash(self.lockfile, "unknown", SPACE_ID, sources=(self.source,))

    def test_generation_inputs_hold_the_job_scope_and_are_frozen(self) -> None:
        pin = self.lockfile.sources[self.source.name]
        courts = tuple(sorted(pin.courts or ()))
        geography = {court: CourtGeography("appeals", None, None) for court in courts}
        name = generation_name(self.collection, self.lockfile.label, SPACE_ID)
        digest = inputs_hash(self.lockfile, self.collection, SPACE_ID, sources=(self.source,))
        inputs = GenerationInputs(
            name, self.collection, self.lockfile.label,
            f"{self.source.name}-{pin.snapshot_date}", courts, geography,
            self.lockfile.pipeline, SPACE_ID, digest,
        )
        self.assertEqual(inputs.name, name)
        self.assertEqual(inputs.collection, self.collection)
        self.assertEqual(inputs.label, self.lockfile.label)
        self.assertEqual(inputs.snapshot, f"{self.source.name}-{pin.snapshot_date}")
        self.assertEqual(inputs.courts, courts)
        self.assertEqual(inputs.geography, geography)
        self.assertEqual(inputs.pipeline, self.lockfile.pipeline)
        self.assertEqual(inputs.embedding_space, SPACE_ID)
        self.assertEqual(inputs.inputs_hash, digest)
        with self.assertRaises(FrozenInstanceError):
            inputs.name = "another-example"  # type: ignore[misc]
