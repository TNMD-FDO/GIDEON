"""The refusal shape and detail returned for a failed command."""

import subprocess
import unittest
from dataclasses import fields

from gideon.host.report import Problem, Timeout, command_detail, refusal


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


class TimeoutProblem(unittest.TestCase):
    def test_timeout_is_a_problem_with_the_same_printed_shape(self) -> None:
        timeout = Timeout("the bound expired", "Check the service, then retry.")

        self.assertIsInstance(timeout, Problem)
        self.assertEqual(tuple(field.name for field in fields(timeout)), ("problem", "fix"))
        self.assertEqual(
            refusal("eval run", timeout.problem, timeout.fix),
            "gideon eval run: the bound expired Fix: Check the service, then retry.",
        )
