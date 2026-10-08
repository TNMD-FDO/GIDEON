"""Keep git auto-maintenance out of test subprocesses.

A detached maintenance process can still write under a fixture's .git/objects
when its temporary directory is removed. This hook is the suite's one home for
preventing that work.
"""

import os
from collections.abc import MutableMapping


def disable_git_auto_maintenance(environment: MutableMapping[str, str]) -> None:
    """Append the git setting after inherited entries unless it is already last."""

    raw_count = environment.get("GIT_CONFIG_COUNT", "0")
    if not raw_count.isascii() or not raw_count.isdecimal():
        raise ValueError(
            "GIT_CONFIG_COUNT must be a non-negative integer; set it to the "
            "number of entries or unset it."
        )
    count = int(raw_count)
    for index in range(count - 1, -1, -1):
        if environment.get(f"GIT_CONFIG_KEY_{index}") == "maintenance.auto":
            if environment.get(f"GIT_CONFIG_VALUE_{index}") == "false":
                return
            break
    environment[f"GIT_CONFIG_KEY_{count}"] = "maintenance.auto"
    environment[f"GIT_CONFIG_VALUE_{count}"] = "false"
    environment["GIT_CONFIG_COUNT"] = str(count + 1)


def pytest_configure(config: object) -> None:
    """Set the rule before pytest imports any test module."""

    disable_git_auto_maintenance(os.environ)
