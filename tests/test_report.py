"""The refusal shape and detail returned for a failed command."""

import subprocess
import unittest

from gideon.host.report import command_detail, refusal


class CommandDetail(unittest.TestCase):
    def test_both_streams_when_both_spoke(self) -> None:
        # A Compose one-off puts its progress on stderr; pgBackRest's error was on stdout.
        both = subprocess.CompletedProcess(["x"], 1, "ERROR: [058]: target timeline\n", " Container created\n")
        self.assertEqual(command_detail(both), "Container created | ERROR: [058]: target timeline")
        self.assertEqual(command_detail(subprocess.CompletedProcess(["x"], 1, "", "boom\n")), "boom")
        self.assertEqual(command_detail(subprocess.CompletedProcess(["x"], 1, "out\n", "")), "out")
        self.assertEqual(command_detail(subprocess.CompletedProcess(["x"], 1, "", "")), "command failed")

    def test_refusal_shape(self) -> None:
        self.assertEqual(refusal("restore", "no set.", "Run it."), "gideon restore: no set. Fix: Run it.")
