"""Subprocess contracts for the release git-tail guard and its derivations."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tools.exportboundary import in_export_tree

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = Path("gideon/__init__.py")
README = Path("README.md")
INDEX = Path("CHANGELOG.md")
INDEX_PREAMBLE = (
    "# Changelog\n\nA major release has a ## Breaking section, and gideon upgrade reads it.\n\n"
)
BASE_INDEX_LINE = "- [v0.1.1](docs/2-changelog/w1_v0.1.1.md) — 2026-01-05 — the base release"


def git(root: Path, *arguments: str) -> str:
    """Run one setup or inspection Git command and return its output."""

    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed with {result.returncode}: {result.stderr}"
        )
    return result.stdout.strip()


def write(path: Path, text: str) -> None:
    """Write one temporary-repository file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read(path: Path) -> str:
    """Read one temporary-repository file."""

    return path.read_text(encoding="utf-8")


def changelog_text(version: str, review: str, subject: str) -> str:
    """Return a changelog file in the release skill's shape."""

    return (
        f"# Changelog - Week 1, 12-01-2026, V. {version}\n\n"
        "**Release Date**: Week 1, 12-01-2026 at 10:00\n"
        f"**Version**: {version} (previously 0.0.0)\n"
        f"**Object**: {subject}\n"
        f"**Code review**: `{review}` (Codex loop, 1 round -> APPROVED)\n\n"
        "## Changes\n\nA fictitious release.\n"
    )


class ReleaseGitTests(TestCase):
    """Exercise release-git from a feature worktree beside its main checkout."""

    def setUp(self) -> None:
        if in_export_tree(ROOT):
            self.skipTest("bin/ is excluded from the public export")
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.primary = self.root / "primary"
        self.worktree = self.root / "cycle"
        self.message = self.root / "message.txt"
        self.script = ROOT / "bin" / "release-git"
        self.primary.mkdir()
        write(self.primary / "base.txt", "base\n")
        write(self.primary / "shared.txt", "base\n")
        write(self.primary / VERSION_FILE, '"""Fixture."""\n\n__version__ = "0.1.1"\n')
        write(self.primary / README, "# Fixture\n\n**Status:** this tree is `v0.1.1`.\n")
        write(self.primary / INDEX, INDEX_PREAMBLE + BASE_INDEX_LINE + "\n")
        write(
            self.primary / "docs/2-changelog/w1_v0.1.1.md",
            "# Changelog - Week 1, 05-01-2026, V. 0.1.1\n",
        )
        git(self.primary, "init", "-b", "main")
        git(self.primary, "config", "user.name", "Release Git Test")
        git(self.primary, "config", "user.email", "release-git@example.test")
        git(self.primary, "add", ".")
        git(self.primary, "commit", "-m", "base")
        git(self.primary, "tag", "v0.1.1")
        git(self.primary, "worktree", "add", str(self.worktree), "-b", "cycle", "main")
        self.write_release("0.1.2")
        self.addCleanup(self._abort_rebase)

    def write_release(self, version: str) -> None:
        """Write the worktree's version, changelog, review, and message for *version*."""

        subject = f"release: v{version} — temporary release"
        self.changelog = self.worktree / f"docs/2-changelog/w1_v{version}.md"
        self.review = self.worktree / f"docs/3-code-review/CR_w1_v{version}.md"
        write(self.worktree / VERSION_FILE, f'"""Fixture."""\n\n__version__ = "{version}"\n')
        write(self.changelog, changelog_text(version, f"docs/3-code-review/CR_w1_v{version}.md", subject))
        write(self.review, f"# Code Review\n\n**Version**: {version}\n")
        write(self.message, f"{subject}\n\nA fictitious release.\n")

    def set_version(self, version: str) -> None:
        """Retype the worktree's __version__ and the message subject alone."""

        write(self.worktree / VERSION_FILE, f'"""Fixture."""\n\n__version__ = "{version}"\n')
        write(self.message, f"release: v{version} — temporary release\n\nA fictitious release.\n")

    def main_takes(self, version: str) -> None:
        """Release *version* on main: the version file, its changelog, its tag."""

        write(self.primary / VERSION_FILE, f'"""Fixture."""\n\n__version__ = "{version}"\n')
        write(
            self.primary / f"docs/2-changelog/w1_v{version}.md",
            f"# Changelog - Week 1, 08-01-2026, V. {version}\n",
        )
        git(self.primary, "add", ".")
        git(self.primary, "commit", "-m", f"release: v{version}")
        git(self.primary, "tag", f"v{version}")

    def run_release(
        self, *arguments: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the real script from the requested checkout."""

        return subprocess.run(
            [str(self.script), *arguments],
            cwd=cwd or self.worktree,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def derived_files(self) -> dict[str, str]:
        """Return the bytes of every file the derivation may write."""

        return {
            str(path.relative_to(self.worktree)): read(path)
            for path in sorted(self.worktree.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        }

    def log_heads(self, root: Path | None = None) -> str:
        """Return the current checkout's commit ids in log order."""

        return git(root or self.worktree, "log", "--format=%H")

    def assert_refusal(self, result: subprocess.CompletedProcess[str]) -> None:
        """Assert the guard's one-line refusal contract."""

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertRegex(result.stderr, r"^release-git: .+\. Fix: .+\n$")

    def _abort_rebase(self) -> None:
        rebase_merge = Path(git(self.worktree, "rev-parse", "--git-path", "rebase-merge"))
        if rebase_merge.is_dir():
            result = subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=self.worktree,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise AssertionError(f"git rebase --abort failed: {result.stderr}")

    def test_first_run_derives_stages_commits_and_rebases_after_main_advances(self) -> None:
        write(self.primary / "main-only.txt", "main change\n")
        git(self.primary, "add", "main-only.txt")
        git(self.primary, "commit", "-m", "main advances")
        write(self.worktree / "first-change.txt", "cycle change\n")

        result = self.run_release(str(self.message))

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        decision = f"release-git: committing from {self.message}"
        self.assertIn(decision, lines)
        for command in (
            "+ git add -A",
            f"+ git commit -F {self.message}",
            "+ git rebase main",
        ):
            self.assertIn(command, lines)
            self.assertGreater(lines.index(command), lines.index(decision))
        subject = self.message.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(git(self.worktree, "log", "-1", "--format=%s"), subject)
        self.assertTrue((self.worktree / "first-change.txt").is_file())
        self.assertTrue((self.worktree / "main-only.txt").is_file())
        self.assertIn("this tree is `v0.1.2`", read(self.worktree / README))
        self.assertEqual(
            read(self.worktree / INDEX),
            INDEX_PREAMBLE
            + "- [v0.1.2](docs/2-changelog/w1_v0.1.2.md) — 2026-01-12 — temporary release\n"
            + BASE_INDEX_LINE
            + "\n",
        )
        self.assertIn("**Version**: 0.1.2 (previously 0.1.1)\n", read(self.changelog))
        self.assertEqual(git(self.worktree, "status", "--porcelain"), "")

    def test_second_run_amends_the_release_commit(self) -> None:
        write(self.worktree / "first-change.txt", "first\n")
        first = self.run_release(str(self.message))
        self.assertEqual(first.returncode, 0, first.stderr)
        write(self.worktree / "second-change.txt", "second\n")

        result = self.run_release(str(self.message))

        self.assertEqual(result.returncode, 0, result.stderr)
        subject = self.message.read_text(encoding="utf-8").splitlines()[0]
        self.assertIn(f"release-git: amending {subject}", result.stdout)
        self.assertIn("+ git commit --amend --no-edit", result.stdout)
        self.assertEqual(git(self.worktree, "rev-list", "--count", "main..HEAD"), "1")
        self.assertTrue((self.worktree / "first-change.txt").is_file())
        self.assertTrue((self.worktree / "second-change.txt").is_file())

    def test_docs_commit_gets_a_new_release_commit_above_it(self) -> None:
        write(self.worktree / "docs-note.md", "fictitious note\n")
        git(self.worktree, "add", "docs-note.md")
        git(self.worktree, "commit", "-m", "docs: temporary note")
        write(self.worktree / "release-change.txt", "release\n")

        result = self.run_release(str(self.message))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"release-git: committing from {self.message}", result.stdout)
        self.assertEqual(git(self.worktree, "rev-list", "--count", "main..HEAD"), "2")
        subject = self.message.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(
            git(self.worktree, "log", "-2", "--format=%s").splitlines(),
            [subject, "docs: temporary note"],
        )

    def test_rebase_conflict_stops_and_leaves_rebase_in_progress(self) -> None:
        write(self.worktree / "shared.txt", "cycle version\n")
        write(self.primary / "shared.txt", "main version\n")
        git(self.primary, "add", "shared.txt")
        git(self.primary, "commit", "-m", "main changes shared file")

        result = self.run_release(str(self.message))

        self.assertEqual(result.returncode, 1)
        self.assertIn("CONFLICT", result.stdout + result.stderr)
        rebase_merge = Path(git(self.worktree, "rev-parse", "--git-path", "rebase-merge"))
        self.assertTrue(rebase_merge.is_dir())

    def test_derive_renames_and_rewrites_when_main_took_the_number(self) -> None:
        self.main_takes("0.1.2")
        taken = self.run_release("--derive")
        self.assert_refusal(taken)
        self.assertIn("__version__ 0.1.2 is not above main's tag v0.1.2", taken.stderr)
        self.set_version("0.1.3")

        result = self.run_release("--derive")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("+ git", result.stdout)
        self.assertEqual(git(self.worktree, "rev-list", "--count", "main..HEAD"), "0")
        self.assertFalse(self.changelog.exists())
        self.assertFalse(self.review.exists())
        changelog = self.worktree / "docs/2-changelog/w1_v0.1.3.md"
        review = self.worktree / "docs/3-code-review/CR_w1_v0.1.3.md"
        self.assertIn(f"release-git: {self.changelog.relative_to(self.worktree)} renamed to docs/2-changelog/w1_v0.1.3.md", result.stdout)
        text = read(changelog)
        self.assertEqual(text.splitlines()[0], "# Changelog - Week 1, 12-01-2026, V. 0.1.3")
        self.assertIn("**Version**: 0.1.3 (previously 0.1.2)\n", text)
        self.assertIn("**Object**: release: v0.1.3 — temporary release\n", text)
        self.assertIn("**Code review**: `docs/3-code-review/CR_w1_v0.1.3.md` (Codex loop", text)
        self.assertEqual(read(review), "# Code Review\n\n**Version**: 0.1.3\n")
        self.assertIn("this tree is `v0.1.3`", read(self.worktree / README))
        self.assertIn(
            "- [v0.1.3](docs/2-changelog/w1_v0.1.3.md) — 2026-01-12 — temporary release\n",
            read(self.worktree / INDEX),
        )
        self.assertNotIn("w1_v0.1.2.md", read(self.worktree / INDEX))

        committed = self.run_release(str(self.message))

        # The version file is the one conflict: both sides moved it from 0.1.1.
        self.assertEqual(committed.returncode, 1)
        self.assertEqual(
            git(self.worktree, "diff", "--name-only", "--diff-filter=U"),
            str(VERSION_FILE),
        )
        self.set_version("0.1.3")
        git(self.worktree, "add", str(VERSION_FILE))
        continued = subprocess.run(
            ["git", "-c", "core.editor=true", "rebase", "--continue"],
            cwd=self.worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        self.assertTrue(changelog.is_file())
        self.assertTrue((self.worktree / "docs/2-changelog/w1_v0.1.2.md").is_file())
        self.assertEqual(git(self.worktree, "rev-list", "--count", "main..HEAD"), "1")
        self.assertIn('__version__ = "0.1.3"', read(self.worktree / VERSION_FILE))

    def test_derive_twice_changes_nothing(self) -> None:
        first = self.run_release("--derive")
        self.assertEqual(first.returncode, 0, first.stderr)
        after_first = self.derived_files()

        second = self.run_release("--derive")

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(after_first, self.derived_files())

    def test_existing_index_line_keeps_its_phrase(self) -> None:
        hand_written = (
            "- [v0.1.2](docs/2-changelog/w1_v0.1.2.md) — 2025-12-31 — a hand-written clause"
        )
        write(self.worktree / INDEX, INDEX_PREAMBLE + hand_written + "\n" + BASE_INDEX_LINE + "\n")

        result = self.run_release("--derive")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            read(self.worktree / INDEX),
            INDEX_PREAMBLE
            + "- [v0.1.2](docs/2-changelog/w1_v0.1.2.md) — 2026-01-12 — a hand-written clause\n"
            + BASE_INDEX_LINE
            + "\n",
        )

    def test_derive_without_release_files_moves_the_readme_tag_alone(self) -> None:
        self.changelog.unlink()
        self.review.unlink()
        index_before = read(self.worktree / INDEX)

        result = self.run_release("--derive")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("this tree is `v0.1.2`", read(self.worktree / README))
        self.assertEqual(index_before, read(self.worktree / INDEX))

    def test_version_not_above_main_tag_refuses_without_writing(self) -> None:
        self.set_version("0.1.1")
        before_files = self.derived_files()
        before = self.log_heads()

        result = self.run_release(str(self.message))

        self.assert_refusal(result)
        self.assertIn("not above main's tag v0.1.1", result.stderr)
        self.assertEqual(before, self.log_heads())
        self.assertEqual(before_files, self.derived_files())

    def test_subject_naming_another_version_refuses(self) -> None:
        write(self.message, "release: v0.1.9 — temporary release\n")
        before_files = self.derived_files()

        result = self.run_release(str(self.message))

        self.assert_refusal(result)
        self.assertIn("names v0.1.9 and __version__ is 0.1.2", result.stderr)
        self.assertEqual(before_files, self.derived_files())

    def test_missing_version_file_refuses(self) -> None:
        (self.worktree / VERSION_FILE).unlink()

        result = self.run_release("--derive")

        self.assert_refusal(result)
        self.assertIn("states no __version__", result.stderr)

    def test_missing_message_file_refuses_without_changing_log(self) -> None:
        before = self.log_heads()

        result = self.run_release(str(self.root / "missing-message.txt"))

        self.assert_refusal(result)
        self.assertEqual(before, self.log_heads())

    def test_main_checkout_refuses_without_changing_log(self) -> None:
        before = self.log_heads(self.primary)

        result = self.run_release(str(self.message), cwd=self.primary)

        self.assert_refusal(result)
        self.assertEqual(before, self.log_heads(self.primary))

    def test_in_progress_rebase_refuses_before_staging_conflicts(self) -> None:
        write(self.worktree / "shared.txt", "cycle committed\n")
        git(self.worktree, "add", "-A")
        git(self.worktree, "commit", "-m", "cycle changes shared file")
        write(self.primary / "shared.txt", "main committed\n")
        git(self.primary, "add", "shared.txt")
        git(self.primary, "commit", "-m", "main changes shared file")
        rebase = subprocess.run(
            ["git", "rebase", "main"],
            cwd=self.worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(rebase.returncode, 0)
        before_status = git(self.worktree, "status", "--porcelain")
        before_unmerged = git(self.worktree, "ls-files", "-u")
        before_log = self.log_heads()

        result = self.run_release(str(self.message))

        self.assert_refusal(result)
        self.assertEqual(before_log, self.log_heads())
        self.assertEqual(before_status, git(self.worktree, "status", "--porcelain"))
        self.assertEqual(before_unmerged, git(self.worktree, "ls-files", "-u"))

    def test_empty_first_line_refuses_without_changing_log(self) -> None:
        self.message.write_text("\nrelease: ignored subject\n", encoding="utf-8")
        before = self.log_heads()

        result = self.run_release(str(self.message))

        self.assert_refusal(result)
        self.assertEqual(before, self.log_heads())

    def test_wrong_argument_count_prints_usage(self) -> None:
        for arguments in ((), (str(self.message), str(self.message))):
            with self.subTest(arguments=arguments):
                result = self.run_release(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("Usage: bin/release-git <message file>", result.stderr)
