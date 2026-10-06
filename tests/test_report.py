"""The refusal shape and detail returned for a failed command."""

import os
import subprocess
import unittest
from dataclasses import fields

from gideon.host import report
from gideon.host.report import Problem, Timeout, command_detail, failure_lines, refusal


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

    def test_command_forms_and_name(self) -> None:
        self.addCleanup(report.set_form_from_environment, os.environ.copy())
        for installed, command_prefix, plain_prefix in (
            (False, "sudo python3 -m gideon", "python3 -m gideon"),
            (True, "gideon", "gideon"),
        ):
            with self.subTest(installed=installed):
                report.set_installed_form(installed)
                self.assertEqual(report.command("apply"), f"{command_prefix} apply")
                self.assertEqual(
                    report.command("registry mirror", sudo=False),
                    f"{plain_prefix} registry mirror",
                )
                self.assertEqual(report.command_name("engine verify"), "gideon engine verify")
                self.assertEqual(
                    report.refusal("eval run", "unavailable", "Retry."),
                    "gideon eval run: unavailable Fix: Retry.",
                )


class FailureLines(unittest.TestCase):
    """A report's failure: its problem line when given, then its fix, nothing collapsed."""

    def test_failure_lines_with_and_without_a_problem(self) -> None:
        self.assertEqual(failure_lines("Repair\nthis"), ("Fix: Repair\nthis",))
        self.assertEqual(
            failure_lines("Repair\nthis", "Problem\nline"),
            ("Problem\nline", "Fix: Repair\nthis"),
        )


class TimeoutProblem(unittest.TestCase):
    def test_timeout_is_a_problem_with_the_same_printed_shape(self) -> None:
        timeout = Timeout("the bound expired", "Check the service, then retry.")

        self.assertIsInstance(timeout, Problem)
        self.assertEqual(tuple(field.name for field in fields(timeout)), ("problem", "fix"))
        self.assertEqual(
            refusal("eval run", timeout.problem, timeout.fix),
            "gideon eval run: the bound expired Fix: Check the service, then retry.",
        )
