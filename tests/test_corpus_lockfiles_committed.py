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

ROOT = Path(__file__).resolve().parent.parent


class CommittedLockfiles(unittest.TestCase):
    """Every shipped pin resolves through the shipped court map and allowlist."""

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
