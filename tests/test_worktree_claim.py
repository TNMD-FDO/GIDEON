"""Subprocess contracts for the primary-checkout worktree claim guard."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tools.exportboundary import in_export_tree

ROOT = Path(__file__).resolve().parents[1]


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


class WorktreeClaimTests(TestCase):
    """Exercise worktree-claim from a synthetic primary checkout."""

    def setUp(self) -> None:
        if in_export_tree(ROOT):
            self.skipTest("bin/ is excluded from the public export")
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.primary = self.root / "primary"
        self.script = self.primary / "bin" / "worktree-claim"
        self.script.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "bin" / "worktree-claim", self.script)
        self.script.chmod(0o755)
        write(self.primary / ".gitignore", "/.venv\n")
        write(self.primary / "seed.txt", "seed\n")
        (self.primary / ".claude" / "worktrees").mkdir(parents=True)
        git(self.primary, "init", "-b", "main")
        git(self.primary, "config", "user.name", "Worktree Claim Test")
        git(self.primary, "config", "user.email", "worktree-claim@example.test")
        git(self.primary, "add", ".")
        git(self.primary, "commit", "-m", "base")
        self.main_head = git(self.primary, "rev-parse", "main")
        git(self.primary, "switch", "-c", "current")
        write(self.primary / "current-only.txt", "current branch\n")
        git(self.primary, "add", "current-only.txt")
        git(self.primary, "commit", "-m", "current branch")
        (self.primary / ".venv").mkdir()

    def run_claim(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the copied script from the synthetic primary checkout."""

        return subprocess.run(
            [str(self.script), *arguments],
            cwd=self.primary,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def assert_created(self, slug: str, kind: str = "feat") -> Path:
        """Assert one requested worktree is created from main."""

        arguments = (slug,) if kind == "feat" else (slug, kind)
        result = self.run_claim(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.primary / ".claude" / "worktrees" / slug
        self.assertEqual(result.stdout.splitlines()[-1], str(target))
        self.assertEqual(git(target, "rev-parse", "HEAD"), self.main_head)
        self.assertEqual(git(target, "branch", "--show-current"), f"{kind}/{slug}")
        self.assertTrue((target / ".venv").is_symlink())
        self.assertEqual((target / ".venv").resolve(), (self.primary / ".venv").resolve())
        return target

    def test_default_run_creates_feat_worktree_from_main(self) -> None:
        self.assert_created("plain-claim")
        self.assertEqual(git(self.primary, "branch", "--show-current"), "current")

    def test_fix_and_hotfix_kinds_create_their_named_branches(self) -> None:
        self.assert_created("fix-claim", "fix")
        self.assert_created("hotfix-claim", "hotfix")

    def test_second_run_refuses_without_changing_repository(self) -> None:
        self.assert_created("duplicate-claim")
        before_status = git(self.primary, "status", "--porcelain")
        before_worktrees = git(self.primary, "worktree", "list", "--porcelain")

        result = self.run_claim("duplicate-claim")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "worktree-claim: a worktree for feat/duplicate-claim exists at "
            f"{self.primary}/.claude/worktrees/duplicate-claim. Fix: enter it with "
            "EnterWorktree by path; the release's merge alone removes one\n",
        )
        self.assertEqual(before_status, git(self.primary, "status", "--porcelain"))
        self.assertEqual(
            before_worktrees, git(self.primary, "worktree", "list", "--porcelain")
        )

    def test_worktree_copy_is_refused(self) -> None:
        copied = self.primary / "x" / ".claude" / "worktrees" / "y" / "bin" / "worktree-claim"
        copied.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "bin" / "worktree-claim", copied)
        copied.chmod(0o755)

        result = subprocess.run(
            [str(copied), "copied-claim"],
            cwd=self.primary,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn(
            "worktree-claim: the script is in a worktree. Fix: run the primary "
            "bin/worktree-claim",
            result.stderr,
        )

    def test_invalid_arguments_print_usage(self) -> None:
        cases = (
            (),
            ("bad/slug",),
            ("other", "unknown-kind"),
            ("too-many", "feat", "extra"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_claim(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("Usage: bin/worktree-claim", result.stderr)
