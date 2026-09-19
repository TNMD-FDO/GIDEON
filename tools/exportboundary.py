"""The public repository's export boundary (slice-1 tickets 56 and 79).

The public repository receives a ``git archive`` of each release tag; the
paths listed here stay in the development repository. ``.gitattributes``
mirrors the list as ``export-ignore`` lines and ``tests/test_export_boundary.py``
holds the two equal. A whole test that reads an excluded path skips in an
exported tree by ``in_export_tree``; a single read uses ``absent_from_export``.
A kept path's absence is never tolerated.
"""

from pathlib import Path

EXCLUDED_PREFIXES = (
    ".scratch",  # the development tracker and its evidence
    "docs/handoffs",  # pre-work handoffs
    "docs/teach",  # the teaching workspace
    "docs/1-plans",  # TRIP implementation plans
    "docs/3-code-review",  # TRIP code reviews
    "docs/5-tuto",  # TRIP tutorials
    "docs/6-memo",  # TRIP engineering memos
    ".claude",  # the agents, hooks, settings, and skills
    "CLAUDE.md",  # the agents' project instructions
    "CONTEXT.md",  # the development vocabulary
    "docs/agents",  # the agents' workflow documents
    "skills-lock.json",  # the vendored skills' pins
    ".out-of-scope",  # rejected enhancements
    "docs/4-unit-tests",  # the testing guide and coverage ledger
    "docs/release-notes/TEMPLATE.md",  # the release-note template
    "docs/box-ledger.md",  # the shared server's ledger
    "tools/tracker.py",  # the tracker board
    "tools/cycles.py",  # the cycles record
    "tests/test_tracker.py",  # the tracker board's tests
    "tests/test_cycles.py",  # the cycles record's tests
    "tests/test_skills_vendored.py",  # the vendored-skills tripwire
    "tests/test_evidence_hygiene.py",  # the tracker assets' secret check
    "tests/test_release_git.py",  # bin/release-git's tests
    "tests/test_worktree_claim.py",  # bin/worktree-claim's tests
    "tests/test_worktree_remove.py",  # bin/worktree-remove's tests
    "tests/test_trip.py",  # bin/trip's tests
    "tests/test_release_export.py",  # bin/release-export's tests
    "eval/seed/prototype-qa",  # harvested QA data pending a CSA ruling
    "eval/sets/eval-v1/judgments/queries.jsonl",  # harvest-derived judgment queries pending a CSA ruling
    "tools/judgments",  # judgment intake tooling that loads the excluded flagger
    "tests/test_judgments_intake.py",  # judgment intake's excluded tests
    ".github/workflows/acceptance.yml",  # the box acceptance workflow
    ".github/workflows/pin-watch.yml",  # the box pin-watch workflow
    ".github/dependabot.yml",  # Dependabot's development-repository trigger
    "bin",  # the development lifecycle launcher, naming excluded paths
)


def is_excluded(relative_posix_path: str) -> bool:
    """Return whether a repository-relative path is an excluded path."""

    return any(
        relative_posix_path == prefix
        or relative_posix_path.startswith(prefix + "/")
        for prefix in EXCLUDED_PREFIXES
    )


def absent_from_export(path: str | Path, root: Path) -> bool:
    """Return whether an excluded repository-relative path is absent under ``root``."""

    relative_posix_path = Path(path).as_posix()
    return is_excluded(relative_posix_path) and not (root / relative_posix_path).exists()


def in_export_tree(root: Path) -> bool:
    """Return whether ``root`` has none of the repository's excluded paths."""

    return not any((root / prefix).exists() for prefix in EXCLUDED_PREFIXES)
