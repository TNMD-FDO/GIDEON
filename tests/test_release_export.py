"""Subprocess contracts for the public release export guard."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tools.exportboundary import in_export_tree

ROOT = Path(__file__).resolve().parents[1]
SUCCESS_LINE = "completed\tsuccess\thttps://example.invalid/run/1"


def git(root: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    """Run one Git setup or inspection command and return its output."""

    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed with {result.returncode}: {result.stderr}"
        )
    return result.stdout.strip()


def ref_sha(
    root: Path, reference: str, environment: dict[str, str] | None = None
) -> str | None:
    """Return a ref's commit id, or None when the ref does not exist."""

    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", reference],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(
            f"git rev-parse {reference} failed with {result.returncode}: {result.stderr}"
        )
    return result.stdout.strip() or None


def write(path: Path, text: str) -> None:
    """Write one temporary-repository file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read(path: Path) -> str:
    """Read one temporary-repository file."""

    return path.read_text(encoding="utf-8")


class ReleaseExportTests(TestCase):
    """Exercise the release export over throwaway local repositories."""

    def setUp(self) -> None:
        if in_export_tree(ROOT):
            self.skipTest("bin/ is absent in an export tree")
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self._build_fixture()

    def _build_fixture(self) -> None:
        """Build the private primary and empty public bare repositories."""

        self.primary = self.root / "primary"
        self.dev = self.root / "remotes/TNMD-FDO/GIDEON-dev.git"
        self.public = self.root / "remotes/TNMD-FDO/GIDEON.git"
        self.stubbin = self.root / "stubbin"
        self.tmp_parent = self.root / "tmp"
        self.global_config = self.root / "gitconfig"
        self.tool_log = self.root / "tools.log"
        self.gh_log = self.root / "gh.log"
        self.stubbin.mkdir(parents=True)
        self.tmp_parent.mkdir()
        write(
            self.global_config,
            "[user]\nname = Export Test User\nemail = export-test@example.test\n",
        )
        write(self.tool_log, "")
        write(self.gh_log, "")
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "GIT_CONFIG_GLOBAL": str(self.global_config),
                "GIT_CONFIG_NOSYSTEM": "1",
                "PATH": f"{self.stubbin}{os.pathsep}{self.environment['PATH']}",
                "RELEASE_EXPORT_TOOL_LOG": str(self.tool_log),
                "RELEASE_EXPORT_GH_LOG": str(self.gh_log),
                "RELEASE_EXPORT_WAIT_SECONDS": "2",
                "RELEASE_EXPORT_POLL_SECONDS": "0",
                "TMPDIR": str(self.tmp_parent),
            }
        )

        for repository in (self.dev, self.public):
            repository.parent.mkdir(parents=True, exist_ok=True)
            git(
                repository.parent,
                "init",
                "--bare",
                str(repository),
                environment=self.environment,
            )
            git(
                repository,
                "symbolic-ref",
                "HEAD",
                "refs/heads/main",
                environment=self.environment,
            )

        write(self.primary / ".gitattributes", "bin/ export-ignore\n")
        write(self.primary / "README.md", "fixture readme\n")
        write(self.primary / "docs/nested/file.txt", "nested fixture\n")
        write(self.primary / "subdir/child.txt", "child fixture\n")
        script = self.primary / "bin/release-export"
        script.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "bin/release-export", script)
        script.chmod(0o755)
        git(self.primary, "init", "-b", "main", environment=self.environment)
        git(self.primary, "add", "-A", environment=self.environment)
        git(self.primary, "commit", "-m", "fixture", environment=self.environment)
        git(self.primary, "tag", "v0.1.1", environment=self.environment)
        git(
            self.primary,
            "remote",
            "add",
            "origin",
            str(self.dev),
            environment=self.environment,
        )
        git(
            self.primary,
            "push",
            "origin",
            "main",
            "v0.1.1",
            environment=self.environment,
        )

        for name in ("ruff", "mypy", "pytest"):
            tool = self.primary / f".venv/bin/{name}"
            write(
                tool,
                "#!/usr/bin/env bash\n"
                "printf '%s %s %s\\n' "
                f"'{name}' \"$*\" \"$PWD\" >> \"${{RELEASE_EXPORT_TOOL_LOG}}\"\n"
                f"exit \"${{RELEASE_EXPORT_{name.upper()}_STATUS:-0}}\"\n",
            )
            tool.chmod(0o755)
        gh = self.stubbin / "gh"
        write(
            gh,
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"${RELEASE_EXPORT_GH_LOG}\"\n"
            "if [[ \"$1\" == repo ]]; then\n"
            "    printf '%s\\n' \"${RELEASE_EXPORT_GH_REPO:-TNMD-FDO/GIDEON PUBLIC}\"\n"
            "elif [[ -n \"${RELEASE_EXPORT_GH_LINE:-}\" ]]; then\n"
            "    printf '%s\\n' \"$RELEASE_EXPORT_GH_LINE\"\n"
            "fi\n",
        )
        gh.chmod(0o755)

    def run_export(
        self, *arguments: str, environment_overrides: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the copied export script with isolated Git and tool settings."""

        environment = self.environment.copy()
        environment["RELEASE_EXPORT_GH_LINE"] = SUCCESS_LINE
        if environment_overrides:
            environment.update(environment_overrides)
        return subprocess.run(
            [str(self.primary / "bin/release-export"), *arguments],
            cwd=self.primary,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def prepare_release(
        self, tag: str, removed: str | None = None, added: str | None = None
    ) -> None:
        """Commit, tag, and push a changed private release."""

        if removed is not None:
            (self.primary / removed).unlink()
        if added is not None:
            write(self.primary / added, f"{tag}\n")
        git(self.primary, "add", "-A", environment=self.environment)
        git(self.primary, "commit", "-m", tag, environment=self.environment)
        git(self.primary, "tag", tag, environment=self.environment)
        git(self.primary, "push", "origin", "main", tag, environment=self.environment)

    def tag_existing_commit(self, tag: str) -> None:
        """Tag the fixture's existing private commit and push only that tag."""

        git(self.primary, "tag", tag, "v0.1.1", environment=self.environment)
        git(
            self.primary,
            "push",
            "origin",
            f"refs/tags/{tag}",
            environment=self.environment,
        )

    def tool_names(self) -> list[str]:
        """Return the check-tool names recorded by the fixture stubs."""

        return [line.split(" ", 1)[0] for line in read(self.tool_log).splitlines()]

    def public_files(self) -> set[str]:
        """Return the tracked file paths in the public main branch."""

        return set(
            git(
                self.public,
                "ls-tree",
                "-r",
                "--name-only",
                "refs/heads/main",
                environment=self.environment,
            ).splitlines()
        )

    def assert_public_empty(self) -> None:
        """Assert that the public repository has no exported refs."""

        self.assertIsNone(ref_sha(self.public, "refs/heads/main", self.environment))
        self.assertIsNone(ref_sha(self.public, "refs/tags/v0.1.1", self.environment))
        self.assertIsNone(ref_sha(self.public, "refs/tags/v0.1.2", self.environment))

    def assert_refusal(self, result: subprocess.CompletedProcess[str]) -> None:
        """Assert the export's refusal shape and non-zero outcome."""

        self.assertEqual(result.returncode, 1)
        self.assertIn("release-export:", result.stderr)
        self.assertIn("Fix:", result.stderr)

    def assert_pre_write_refusal(self, result: subprocess.CompletedProcess[str]) -> None:
        """Assert a refusal that ran no export or check command."""

        self.assert_refusal(result)
        self.assertEqual(result.stdout, "")

    def assert_one_temporary_directory(self) -> Path:
        """Return the one kept export directory under the fixture TMPDIR."""

        entries = list(self.tmp_parent.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].is_dir())
        return entries[0]

    def test_first_export_into_empty_public_repository(self) -> None:
        """Cover Section 7's first export and archive boundary case."""

        result = self.run_export("v0.1.1")

        self.assertEqual(result.returncode, 0, result.stderr)
        public_commit = ref_sha(self.public, "refs/heads/main", self.environment)
        self.assertIsNotNone(public_commit)
        self.assertEqual(
            ref_sha(self.public, "refs/tags/v0.1.1", self.environment), public_commit
        )
        self.assertEqual(
            git(self.public, "log", "-1", "--format=%s", environment=self.environment),
            "v0.1.1",
        )
        self.assertEqual(
            self.public_files(),
            {".gitattributes", "README.md", "docs/nested/file.txt", "subdir/child.txt"},
        )
        self.assertNotIn("bin/release-export", self.public_files())
        self.assertEqual(self.tool_names(), ["ruff", "mypy", "pytest"])
        for line in read(self.tool_log).splitlines():
            self.assertNotIn(str(self.primary), line)
            self.assertNotIn(str(self.public), line)
            self.assertTrue(line.split()[-1].startswith(str(self.tmp_parent)))
        self.assertIn("https://example.invalid/run/1", result.stdout)
        self.assertIn("--jq", read(self.gh_log))
        self.assertEqual(list(self.tmp_parent.iterdir()), [])

    def test_second_export_after_change_has_previous_parent(self) -> None:
        """Cover Section 7's changed-tree export and parent lineage."""

        first = self.run_export("v0.1.1")
        self.assertEqual(first.returncode, 0, first.stderr)
        previous = ref_sha(self.public, "refs/heads/main", self.environment)
        self.assertIsNotNone(previous)
        self.prepare_release("v0.1.2", "README.md", "docs/new.txt")
        second = self.run_export("v0.1.2")

        self.assertEqual(second.returncode, 0, second.stderr)
        current = ref_sha(self.public, "refs/heads/main", self.environment)
        self.assertIsNotNone(current)
        self.assertEqual(
            git(
                self.public,
                "rev-parse",
                "refs/heads/main^",
                environment=self.environment,
            ),
            previous,
        )
        self.assertNotEqual(current, previous)
        self.assertNotIn("README.md", self.public_files())
        self.assertIn("docs/new.txt", self.public_files())

    def test_archive_equal_to_last_export_still_commits(self) -> None:
        """Cover Section 7's empty-content export commit."""

        first = self.run_export("v0.1.1")
        self.assertEqual(first.returncode, 0, first.stderr)
        previous = ref_sha(self.public, "refs/heads/main", self.environment)
        self.assertIsNotNone(previous)
        self.tag_existing_commit("v0.1.2")

        second = self.run_export("v0.1.2")

        self.assertEqual(second.returncode, 0, second.stderr)
        current = ref_sha(self.public, "refs/heads/main", self.environment)
        self.assertIsNotNone(current)
        self.assertEqual(
            git(self.public, "rev-list", "--count", "main", environment=self.environment),
            "2",
        )
        self.assertEqual(
            git(self.public, "rev-parse", "main^", environment=self.environment), previous
        )
        self.assertEqual(
            ref_sha(self.public, "refs/tags/v0.1.2", self.environment), current
        )

    def test_re_run_of_exported_tag_only_waits(self) -> None:
        """Cover Section 7's already-exported confirmation path."""

        first = self.run_export("v0.1.1")
        self.assertEqual(first.returncode, 0, first.stderr)
        previous = ref_sha(self.public, "refs/heads/main", self.environment)
        self.assertIsNotNone(previous)
        write(self.tool_log, "")

        second = self.run_export(
            "v0.1.1",
            environment_overrides={
                "RELEASE_EXPORT_RUFF_STATUS": "1",
                "RELEASE_EXPORT_MYPY_STATUS": "1",
                "RELEASE_EXPORT_PYTEST_STATUS": "1",
            },
        )

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            ref_sha(self.public, "refs/heads/main", self.environment), previous
        )
        self.assertEqual(read(self.tool_log), "")
        self.assertIn("run list", read(self.gh_log))
        self.assertEqual(list(self.tmp_parent.iterdir()), [])

    def test_primary_local_identity_is_used(self) -> None:
        """Cover Section 7's identity captured from the primary config."""

        write(self.global_config, "")
        git(
            self.primary,
            "config",
            "user.name",
            "Local Export User",
            environment=self.environment,
        )
        git(
            self.primary,
            "config",
            "user.email",
            "local-export@example.test",
            environment=self.environment,
        )

        result = self.run_export("v0.1.1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            git(
                self.public,
                "log",
                "-1",
                "--format=%an <%ae>",
                environment=self.environment,
            ),
            "Local Export User <local-export@example.test>",
        )

    def test_lineage_invariant_refuses_hand_commit(self) -> None:
        """Cover Section 7's public-main lineage invariant."""

        self.tag_existing_commit("v0.1.2")
        hand = self.root / "hand"
        git(self.root, "clone", str(self.public), str(hand), environment=self.environment)
        write(hand / "hand.txt", "unexported\n")
        git(hand, "add", "hand.txt", environment=self.environment)
        git(hand, "commit", "-m", "hand commit", environment=self.environment)
        git(hand, "push", "origin", "main", environment=self.environment)
        before = ref_sha(self.public, "refs/heads/main", self.environment)

        result = self.run_export("v0.1.2")

        self.assert_refusal(result)
        self.assertEqual(
            ref_sha(self.public, "refs/heads/main", self.environment), before
        )
        self.assertIsNone(ref_sha(self.public, "refs/tags/v0.1.2", self.environment))
        self.assertIn("no export made", result.stderr)
        self.assertIn("moves the public main back by hand", result.stderr)

    def test_lineage_invariant_refuses_tagged_hand_commit(self) -> None:
        """A v* tag on a commit whose message is not that tag is no export."""

        self.tag_existing_commit("v0.1.2")
        hand = self.root / "hand"
        git(self.root, "clone", str(self.public), str(hand), environment=self.environment)
        write(hand / "hand.txt", "unexported\n")
        git(hand, "add", "hand.txt", environment=self.environment)
        git(hand, "commit", "-m", "hand commit", environment=self.environment)
        git(hand, "tag", "v0.0.9", environment=self.environment)
        git(hand, "push", "origin", "main", "v0.0.9", environment=self.environment)
        before = ref_sha(self.public, "refs/heads/main", self.environment)

        result = self.run_export("v0.1.2")

        self.assert_refusal(result)
        self.assertIn("no export made", result.stderr)
        self.assertEqual(ref_sha(self.public, "refs/heads/main", self.environment), before)
        self.assertIsNone(ref_sha(self.public, "refs/tags/v0.1.2", self.environment))

    def test_public_name_redirecting_elsewhere_refuses_before_write(self) -> None:
        """The old name redirecting to the development repository is never the target."""

        result = self.run_export(
            "v0.1.1",
            environment_overrides={"RELEASE_EXPORT_GH_REPO": "TNMD-FDO/GIDEON-dev PRIVATE"},
        )

        self.assert_pre_write_refusal(result)
        self.assertIn("create the public TNMD-FDO/GIDEON", result.stderr)
        self.assert_public_empty()
        self.assertEqual(self.tool_names(), [])
        self.assertNotIn("run list", read(self.gh_log))

    def test_server_side_refusal_is_atomic(self) -> None:
        """Cover Section 7's atomic push refusal and kept directory."""

        hook = self.public / "hooks/update"
        write(
            hook,
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == refs/heads/main ]]; then\n'
            "    printf '%s\\n' 'public main is protected' >&2\n"
            "    exit 1\n"
            "fi\n",
        )
        hook.chmod(0o755)

        result = self.run_export("v0.1.1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(ref_sha(self.public, "refs/heads/main", self.environment))
        self.assertIsNone(ref_sha(self.public, "refs/tags/v0.1.1", self.environment))
        self.assertIn("public main is protected", result.stderr)
        self.assertIn("read the public main", result.stderr)
        self.assert_one_temporary_directory()

    def test_red_check_stops_before_pytest_and_keeps_directory(self) -> None:
        """Cover Section 7's first red check and retained temporary directory."""

        result = self.run_export(
            "v0.1.1", environment_overrides={"RELEASE_EXPORT_MYPY_STATUS": "1"}
        )

        self.assert_refusal(result)
        self.assertEqual(self.tool_names(), ["ruff", "mypy"])
        self.assert_public_empty()
        kept = self.assert_one_temporary_directory()
        self.assertIn(str(kept), result.stderr)

    def test_red_run_refuses_with_hotfix_fix(self) -> None:
        """Cover Section 7's completed non-success CI run."""

        result = self.run_export(
            "v0.1.1",
            environment_overrides={
                "RELEASE_EXPORT_GH_LINE": "completed\tfailure\thttps://example.invalid/run/1"
            },
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("https://example.invalid/run/1", result.stdout)
        self.assertIn("TRIP-hotfix", result.stderr)
        kept = self.assert_one_temporary_directory()
        self.assertIn(str(kept), result.stderr)

    def test_absent_run_refuses_with_manual_command(self) -> None:
        """Cover Section 7's bounded wait without a completed run."""

        result = self.run_export(
            "v0.1.1", environment_overrides={"RELEASE_EXPORT_GH_LINE": ""}
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("gh run list --repo TNMD-FDO/GIDEON --workflow ci --commit", result.stderr)
        self.assert_one_temporary_directory()

    def test_origin_naming_other_repository_refuses_before_write(self) -> None:
        """Cover Section 7's wrong repository origin guard."""

        git(
            self.primary,
            "remote",
            "set-url",
            "origin",
            str(self.root / "remotes/TNMD-FDO/OTHER.git"),
            environment=self.environment,
        )

        result = self.run_export("v0.1.1")

        self.assert_pre_write_refusal(result)
        self.assert_public_empty()
        self.assertEqual(self.tool_names(), [])

    def test_origin_suffix_without_separator_refuses(self) -> None:
        """Cover Section 7's separator-aware origin suffix guard."""

        bad_origin = self.root / "remotes/evilTNMD-FDO/GIDEON-dev.git"
        git(
            self.primary,
            "remote",
            "set-url",
            "origin",
            str(bad_origin),
            environment=self.environment,
        )

        result = self.run_export("v0.1.1")

        self.assert_pre_write_refusal(result)
        self.assert_public_empty()
        self.assertEqual(self.tool_names(), [])

    def test_local_tag_absent_refuses_before_write(self) -> None:
        """Cover Section 7's missing local tag guard."""

        result = self.run_export("v0.1.2")

        self.assert_pre_write_refusal(result)
        self.assert_public_empty()
        self.assertEqual(self.tool_names(), [])

    def test_tag_not_on_origin_refuses_before_write(self) -> None:
        """Cover Section 7's private-origin tag guard."""

        git(self.primary, "tag", "v0.1.2", environment=self.environment)

        result = self.run_export("v0.1.2")

        self.assert_pre_write_refusal(result)
        self.assert_public_empty()
        self.assertEqual(self.tool_names(), [])

    def test_tag_not_on_main_refuses_before_write(self) -> None:
        """Cover Section 7's main ancestry guard."""

        git(self.primary, "checkout", "-b", "side", environment=self.environment)
        write(self.primary / "side.txt", "side\n")
        git(self.primary, "add", "side.txt", environment=self.environment)
        git(self.primary, "commit", "-m", "side", environment=self.environment)
        git(self.primary, "tag", "v0.1.2", environment=self.environment)
        git(
            self.primary,
            "push",
            "origin",
            "side",
            "v0.1.2",
            environment=self.environment,
        )

        result = self.run_export("v0.1.2")

        self.assert_pre_write_refusal(result)
        self.assert_public_empty()
        self.assertEqual(self.tool_names(), [])

    def test_identity_unset_refuses_before_write(self) -> None:
        """Cover Section 7's missing identity guard."""

        write(self.global_config, "")

        result = self.run_export("v0.1.1")

        self.assert_pre_write_refusal(result)
        self.assert_public_empty()
        self.assertEqual(self.tool_names(), [])

    def test_worktree_copy_refuses_before_write(self) -> None:
        """Cover Section 7's worktree-copy guard."""

        copied = self.primary / ".claude/worktrees/x/bin/release-export"
        copied.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "bin/release-export", copied)
        copied.chmod(0o755)

        result = subprocess.run(
            [str(copied), "v0.1.1"],
            cwd=self.primary,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assert_pre_write_refusal(result)
        self.assert_public_empty()
        self.assertEqual(self.tool_names(), [])

    def test_wrong_argument_count_refuses_with_usage(self) -> None:
        """Cover Section 7's argument-count usage exits."""

        for arguments in ((), ("v0.1.1", "extra")):
            with self.subTest(arguments=arguments):
                result = self.run_export(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("Usage: bin/release-export <tag>", result.stderr)

    def test_malformed_tag_refuses_with_usage(self) -> None:
        """Cover Section 7's tag-grammar usage exit."""

        for tag in ("0.1.1", "v0.1", "v1.2.3.4"):
            with self.subTest(tag=tag):
                result = self.run_export(tag)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("Usage: bin/release-export <tag>", result.stderr)
