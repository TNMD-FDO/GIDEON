"""The public repository's export boundary (slice-1 ticket 56).

The public repository receives a ``git archive`` of each release tag; the
paths listed here stay in the development repository. ``.gitattributes``
mirrors the list as ``export-ignore`` lines and ``tests/test_export_boundary.py``
holds the two equal; ``in_export_tree`` is how a test that reads an excluded
path recognises an exported tree and skips there.
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
    ".claude/skills",  # vendored skills without export licensing coverage
    "eval/seed/prototype-qa",  # harvested QA data pending a CSA ruling
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


def in_export_tree(root: Path) -> bool:
    """Return whether ``root`` has none of the repository's excluded paths."""

    return not any((root / prefix).exists() for prefix in EXCLUDED_PREFIXES)
