"""Checks for every corpus lockfile shipped in the checkout."""

import unittest
from pathlib import Path

from gideon.host.corpus.lockfile import (
    check_courts,
    check_egress_hosts,
    read_lockfile_directory,
    render_errors,
)
from gideon.host.courts import load_court_map
from gideon.host.egress import load_egress_allowlist
from gideon.host.models import load_models_lock
from gideon.host.render.worker import PIPELINE_VERSION

ROOT = Path(__file__).resolve().parent.parent


class CommittedLockfiles(unittest.TestCase):
    """Every shipped pin resolves through the shipped court map and allowlist."""

    def test_each_lines_newest_lockfile_names_the_release_pipeline(self) -> None:
        directory = read_lockfile_directory(ROOT / "corpus/lockfiles")
        self.assertFalse(directory.errors, render_errors(directory.errors))
        self.assertTrue(directory.lockfiles, "The release must ship a corpus lockfile.")
        for line in {item.line for item in directory.lockfiles}:
            with self.subTest(line=line):
                newest = directory.newest_on(line)
                assert newest is not None
                self.assertEqual(newest.pipeline, PIPELINE_VERSION)

    def test_lockfile_lines_and_profile_lines_agree(self) -> None:
        directory = read_lockfile_directory(ROOT / "corpus/lockfiles")
        self.assertFalse(directory.errors, render_errors(directory.errors))
        models = load_models_lock(ROOT / "models.lock")
        self.assertTrue(models.ok, models.errors)
        assert models.lock is not None
        lockfile_lines = {item.line for item in directory.lockfiles}
        profile_lines = {profile.corpus_line for profile in models.lock.profiles}
        self.assertFalse(lockfile_lines - profile_lines, "Every committed lockfile line must be named by a hardware profile.")
        self.assertFalse(profile_lines - lockfile_lines, "Every hardware profile's corpus line must have a committed lockfile.")

    def test_all_committed_lockfiles(self) -> None:
        courts = load_court_map(ROOT / "courts.yaml")
        allowlist = load_egress_allowlist(ROOT / "config/egress.yaml")
        self.assertTrue(courts.ok)
        self.assertTrue(allowlist.ok)
        assert courts.court_map is not None
        assert allowlist.allowlist is not None

        directory = read_lockfile_directory(ROOT / "corpus/lockfiles")
        self.assertFalse(directory.errors, render_errors(directory.errors))
        for lockfile in directory.lockfiles:
            with self.subTest(label=lockfile.label):
                self.assertEqual(
                    check_courts(lockfile, courts.court_map), (),
                    "A lockfile court must resolve in courts.yaml.",
                )
                self.assertEqual(
                    check_egress_hosts(lockfile, allowlist.allowlist), (),
                    "A lockfile URL host must be in the corpus allowlist.",
                )
