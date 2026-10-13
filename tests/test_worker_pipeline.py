"""The worker pipeline registry stays coupled to the release locks."""

import unittest
from pathlib import Path

from gideon.host.images import BuiltImagePin, load_image_lock
from gideon.host.models import load_models_lock
from gideon.host.weights import snapshot_path
from gideon.worker.pipeline import (
    PIPELINE_VERSION,
    PIPELINES,
    VERSION_PATTERN,
    tokenizer_paths,
)

ROOT = Path(__file__).resolve().parent.parent


class PipelineRegistry(unittest.TestCase):
    def test_registered_inputs_match_the_locks(self) -> None:
        models = load_models_lock(ROOT / "models.lock")
        images = load_image_lock(ROOT / "images.lock")
        self.assertTrue(models.ok, models.errors)
        self.assertTrue(images.ok, images.errors)
        assert models.lock is not None and images.lock is not None
        profile = models.lock.profile(models.lock.reference)
        self.assertIsNotNone(profile)
        assert profile is not None
        embed = profile.model("embed")
        self.assertIsNotNone(embed)
        assert embed is not None
        image = next(pin for pin in images.lock.images if pin.name == "gideon")
        self.assertIsInstance(image, BuiltImagePin)
        assert isinstance(image, BuiltImagePin)
        pinned_files = {file.path: file.sha256 for file in embed.files}

        for version, entry in PIPELINES.items():
            with self.subTest(version=version):
                self.assertEqual(entry.tokenizer.repository, embed.repo)
                self.assertEqual(entry.tokenizer.revision, embed.revision)
                self.assertEqual(
                    {name for name, _digest in entry.tokenizer.files},
                    {"tokenizer.json", "vocab.json", "merges.txt"},
                )
                self.assertEqual(len(entry.tokenizer.files), 3)
                self.assertEqual(
                    dict(entry.tokenizer.files),
                    {name: pinned_files[name] for name, _digest in entry.tokenizer.files},
                )
                self.assertEqual(entry.eyecite_version, image.build_args["EYECITE_VERSION"])

    def test_versions_and_sizes(self) -> None:
        self.assertIn(PIPELINE_VERSION, PIPELINES)
        for version, entry in PIPELINES.items():
            with self.subTest(version=version):
                self.assertIsNotNone(VERSION_PATTERN.fullmatch(version))
                self.assertEqual(entry.version, version)
                self.assertGreater(entry.chunk_minimum, 0)
                self.assertLessEqual(entry.chunk_minimum, entry.chunk_target)
                self.assertLessEqual(entry.chunk_target, entry.chunk_maximum)
        self.assertIsNone(VERSION_PATTERN.fullmatch("next"))

    def test_tokenizer_paths_match_the_weights_layout(self) -> None:
        root = Path("/fictitious/weights")
        for version, entry in PIPELINES.items():
            with self.subTest(version=version):
                self.assertEqual(
                    tokenizer_paths(entry.tokenizer, root),
                    tuple(snapshot_path(entry.tokenizer.repository, entry.tokenizer.revision,
                                        filename, root) for filename, _digest in entry.tokenizer.files),
                )
