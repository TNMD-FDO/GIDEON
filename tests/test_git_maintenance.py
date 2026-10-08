"""Hold the suite's git auto-maintenance rule against real subprocess traces.

A detached maintenance process can keep writing into a temporary repository
after a git command returns, racing its fixture's cleanup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from conftest import disable_git_auto_maintenance


def git(
    root: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
    expect_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one git command and judge its return code."""

    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=dict(os.environ if environment is None else environment),
        capture_output=True,
        text=True,
        check=False,
    )
    if expect_failure:
        if result.returncode == 0:
            raise AssertionError(f"git {' '.join(arguments)} unexpectedly succeeded")
    elif result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed with {result.returncode}: {result.stderr}"
        )
    return result


class GitMaintenanceEnvironmentTests(TestCase):
    """Check the hook's ordered git configuration entries."""

    def test_empty_environment(self) -> None:
        environment: dict[str, str] = {}
        disable_git_auto_maintenance(environment)
        self.assertEqual(
            environment,
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "maintenance.auto",
                "GIT_CONFIG_VALUE_0": "false",
            },
        )

    def test_foreign_entries_are_preserved(self) -> None:
        environment = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "color.ui",
            "GIT_CONFIG_VALUE_0": "never",
            "GIT_CONFIG_KEY_1": "commit.gpgsign",
            "GIT_CONFIG_VALUE_1": "false",
        }
        original = environment.copy()
        disable_git_auto_maintenance(environment)
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(environment["GIT_CONFIG_KEY_2"], "maintenance.auto")
        self.assertEqual(environment["GIT_CONFIG_VALUE_2"], "false")
        for key, value in original.items():
            if key != "GIT_CONFIG_COUNT":
                self.assertEqual(environment[key], value)

    def test_last_false_entry_is_idempotent(self) -> None:
        environment: dict[str, str] = {}
        disable_git_auto_maintenance(environment)
        original = environment.copy()
        disable_git_auto_maintenance(environment)
        self.assertEqual(environment, original)

    def test_later_true_entry_is_overridden(self) -> None:
        environment = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "maintenance.auto",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "maintenance.auto",
            "GIT_CONFIG_VALUE_1": "true",
        }
        disable_git_auto_maintenance(environment)
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(environment["GIT_CONFIG_KEY_2"], "maintenance.auto")
        self.assertEqual(environment["GIT_CONFIG_VALUE_2"], "false")

    def test_malformed_count_refuses_with_fix(self) -> None:
        for value in ("", "-1", "not-a-number"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ValueError, "GIT_CONFIG_COUNT.*set it to the number of entries or unset it"
                ),
            ):
                disable_git_auto_maintenance({"GIT_CONFIG_COUNT": value})

    def test_this_process_carries_the_entry(self) -> None:
        count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        self.assertTrue(
            any(
                os.environ.get(f"GIT_CONFIG_KEY_{index}") == "maintenance.auto"
                and os.environ.get(f"GIT_CONFIG_VALUE_{index}") == "false"
                for index in range(count)
            ),
            "this pytest run carries no maintenance.auto false entry; run without "
            "--noconftest so tests/conftest.py's pytest_configure hook sets it",
        )


class GitMaintenanceTraceTests(TestCase):
    """Read the parent git process's trace2 child starts."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.name", "Fixture User")
        git(self.repository, "config", "user.email", "fixture@example.test")
        (self.repository / "conflict.txt").write_text("base\n", encoding="utf-8")
        git(self.repository, "add", "conflict.txt")
        git(self.repository, "commit", "-m", "base")

    def events(self, trace: Path, command: str) -> list[dict[str, object]]:
        """Read a trace and require an event from the command under test."""

        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(
            any(event.get("event") == "cmd_name" and event.get("name") == command for event in events),
            f"trace did not record git {command}",
        )
        return events

    def maintenance_children(self, events: list[dict[str, object]]) -> list[list[str]]:
        """Return maintenance child argv emitted by the parent git process."""

        children: list[list[str]] = []
        for event in events:
            if event.get("event") != "child_start":
                continue
            argv = event.get("argv")
            self.assertIsInstance(argv, list)
            if isinstance(argv, list) and argv[:2] == ["git", "maintenance"]:
                children.append(argv)
        return children

    def test_commit_does_not_start_maintenance(self) -> None:
        (self.repository / "changed.txt").write_text("changed\n", encoding="utf-8")
        git(self.repository, "add", "changed.txt")
        trace = self.root / "commit.trace"
        git(
            self.repository,
            "commit",
            "-m",
            "changed",
            environment={**os.environ, "GIT_TRACE2_EVENT": str(trace)},
        )
        self.assertEqual(self.maintenance_children(self.events(trace, "commit")), [])

    def test_up_to_date_rebase_does_not_start_maintenance(self) -> None:
        git(self.repository, "switch", "-c", "topic")
        (self.repository / "changed.txt").write_text("changed\n", encoding="utf-8")
        git(self.repository, "add", "changed.txt")
        git(self.repository, "commit", "-m", "topic change")
        trace = self.root / "rebase.trace"
        git(
            self.repository,
            "rebase",
            "main",
            environment={**os.environ, "GIT_TRACE2_EVENT": str(trace)},
        )
        self.assertEqual(self.maintenance_children(self.events(trace, "rebase")), [])

    def test_rebase_abort_does_not_start_maintenance(self) -> None:
        git(self.repository, "switch", "-c", "topic")
        (self.repository / "conflict.txt").write_text("topic\n", encoding="utf-8")
        git(self.repository, "add", "conflict.txt")
        git(self.repository, "commit", "-m", "topic change")
        git(self.repository, "switch", "main")
        (self.repository / "conflict.txt").write_text("main\n", encoding="utf-8")
        git(self.repository, "add", "conflict.txt")
        git(self.repository, "commit", "-m", "main change")
        git(self.repository, "switch", "topic")
        git(self.repository, "rebase", "main", expect_failure=True)
        trace = self.root / "abort.trace"
        git(
            self.repository,
            "rebase",
            "--abort",
            environment={**os.environ, "GIT_TRACE2_EVENT": str(trace)},
        )
        self.assertEqual(self.maintenance_children(self.events(trace, "rebase")), [])

    def test_foreground_control_starts_maintenance(self) -> None:
        (self.repository / "changed.txt").write_text("changed\n", encoding="utf-8")
        git(self.repository, "add", "changed.txt")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key != "GIT_CONFIG_COUNT"
            and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        }
        environment.update(
            {
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": "maintenance.auto",
                "GIT_CONFIG_VALUE_0": "true",
                "GIT_CONFIG_KEY_1": "maintenance.autoDetach",
                "GIT_CONFIG_VALUE_1": "false",
                "GIT_CONFIG_KEY_2": "gc.autoDetach",
                "GIT_CONFIG_VALUE_2": "false",
            }
        )
        trace = self.root / "control.trace"
        environment["GIT_TRACE2_EVENT"] = str(trace)
        git(self.repository, "commit", "-m", "control", environment=environment)
        children = self.maintenance_children(self.events(trace, "commit"))
        self.assertEqual(len(children), 1)
        self.assertNotIn("--detach", children[0])
