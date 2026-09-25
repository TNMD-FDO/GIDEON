"""The public repository's export boundary (slice-1 tickets 56, 79, 87–89).

The public repository receives a ``git archive`` of each release tag; the
paths listed here are omitted from the archive. ``.gitattributes``
mirrors the list as ``export-ignore`` lines and ``tests/test_export_boundary.py``
holds the two equal; the tests also import ``is_file_prefix`` and
``text_pattern``. A whole test that reads an excluded path skips in an
exported tree by ``in_export_tree``; a single read uses ``absent_from_export``.
A kept path's absence is never tolerated.
"""

import re
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
    "tools/proofs.py",  # the proofs queue
    "tools/archi.py",  # the architecture index check
    "tests/test_tracker.py",  # the tracker board's tests
    "tests/test_cycles.py",  # the cycles record's tests
    "tests/test_proofs.py",  # the proofs queue's tests
    "tests/test_archi.py",  # the architecture index check's tests
    "tests/test_skills_vendored.py",  # the vendored-skills tripwire
    "tests/test_evidence_hygiene.py",  # the tracker assets' secret check
    "tests/test_release_git.py",  # bin/release-git's tests
    "tests/test_release_lock.py",  # bin/release-lock's tests
    "tests/test_export_tree_check.py",  # bin/export-tree-check's tests
    "tests/test_venv_build.py",  # bin/venv-build's tests
    "tests/test_worktree_claim.py",  # bin/worktree-claim's tests
    "tests/test_worktree_remove.py",  # bin/worktree-remove's tests
    "tests/test_trip.py",  # bin/trip's tests
    "tests/test_dispatch.py",  # bin/dispatch's tests
    "tests/test_release_export.py",  # bin/release-export's tests
    "tests/test_codex_rounds.py",  # the Codex rounds record's tests
    "eval/seed/prototype-qa",  # harvested QA data pending a CSA ruling
    "eval/sets/eval-v1/judgments/queries.jsonl",  # harvest-derived judgment queries pending a CSA ruling
    "eval/sets/eval-v1/research-qa/harvest.jsonl",  # harvest-derived research questions pending the same ruling
    "tools/judgments",  # judgment intake tooling that loads the excluded flagger
    "tools/signoffs",  # the sign-off kit tooling
    "tests/test_judgments_intake.py",  # judgment intake's excluded tests
    "tests/test_judgments_kit.py",  # judgment packet and grades intake tests
    "tests/test_signoffs_kit.py",  # the sign-off kit excluded tests
    "eval/sets/eval-v1/build-gates/extraction.jsonl",  # the labelled harvest questions pending the same ruling
    "eval/sets/eval-v1/build-gates/extraction-variants.jsonl",  # the harvest questions' variants, pending the same ruling
    "eval/sets/eval-v1/slices/extraction/harvest.ids",  # the harvest slice ids must leave with their excluded cases
    "eval/sets/eval-v1/slices/smoke/harvest.ids",  # the smoke harvest ids must leave with their excluded cases
    "eval/sets/eval-v1/slices/judgments",  # the judgments slice ids must leave with their excluded queries
    "eval/reference/extraction/harvest.json",  # the harvest reference must leave with its id list
    "eval/reference/smoke/harvest.json",  # the smoke harvest reference must leave with its id list
    ".github/workflows/acceptance.yml",  # the box acceptance workflow
    ".github/workflows/pin-watch.yml",  # the box pin-watch workflow
    ".github/dependabot.yml",  # Dependabot's development-repository trigger
    "bin",  # the development lifecycle launcher, naming excluded paths
    "docs/runbooks/pin-watch-app-setup.md",  # the pin-watch setup runbook
    "docs/runbooks/ci-runner.md",  # the self-hosted runner runbook
    "tests/test_export_names.py",  # the exported-text tripwire
    "README.dev.md",  # the private documentation index
    "docs/2-changelog",  # the engineering record, one file per release
    "docs/ARCHI.md",  # the architecture map
    "docs/ARCHI-rules.md",  # the map's rules and budgets
    "docs/archi",  # the architecture leaves
    "docs/adr",  # the decision records
    "docs/research",  # the research notes
    "tests/test_archi_budget.py",  # the map and leaves' contract
    "tests/test_research_notes.py",  # the research notes' contract
)


def is_excluded(relative_posix_path: str) -> bool:
    """Return whether a repository-relative path is an excluded path."""

    return any(
        relative_posix_path == prefix
        or relative_posix_path.startswith(prefix + "/")
        for prefix in EXCLUDED_PREFIXES
    )


def is_file_prefix(root: Path, prefix: str) -> bool:
    """Return whether an existing prefix is a file; absent prefixes are directories."""

    return (root / prefix).is_file()


def text_pattern(root: Path, prefix: str) -> re.Pattern[str]:
    """Build a text matcher for a path prefix using its shape under ``root``."""

    if "/" not in prefix and not prefix.startswith("."):
        # A bare top-level name (`bin`, `CLAUDE.md`) is a path only at a path's
        # start: not after a path character, so `/usr/bin` and `.venv/bin` are not it.
        return re.compile(
            r"(?<![A-Za-z0-9_.\\/'\"-])"
            + re.escape(prefix)
            + r"(?:/|(?![A-Za-z0-9_.-]))"
        )
    if is_file_prefix(root, prefix):
        return re.compile(re.escape(prefix) + r"(?![A-Za-z0-9_./-])")
    return re.compile(re.escape(prefix) + r"(?:/|(?![A-Za-z0-9_.-]))")


def absent_from_export(path: str | Path, root: Path) -> bool:
    """Return whether an excluded repository-relative path is absent under ``root``."""

    relative_posix_path = Path(path).as_posix()
    return is_excluded(relative_posix_path) and not (root / relative_posix_path).exists()


def in_export_tree(root: Path) -> bool:
    """Return whether ``root`` has none of the repository's excluded paths."""

    return not any((root / prefix).exists() for prefix in EXCLUDED_PREFIXES)
