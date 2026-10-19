"""Resolve the rendered directory and secret directory for an evaluation stack.

The engine and the run's record stay production's on either stack, since the
sibling shares production's engine and the harness keeps one record. The
selected stack's rendered directory serves its turns, record, and stores surfaces.
"""

from dataclasses import dataclass
from pathlib import Path

from gideon.host import secrets
from gideon.host.render.ci import CI_ROOT, CI_SECRETS_DIR, CI_STACK, PRODUCTION_STACK
from gideon.host.sysio import PathLike


@dataclass(frozen=True, slots=True)
class StackPaths:
    """The selected stack's paths and command-line flag fragment.

    ``turns_dir`` is its rendered directory, read by turns, record, and stores
    surfaces; the engine and the run's record stay production's.
    """

    name: str
    turns_dir: Path
    secrets_dir: Path | None
    flag_fragment: str


def flag_fragment(name: str) -> str:
    """The stack's fragment of a retry command, read without selecting anything."""

    return "" if name == PRODUCTION_STACK else f" --stack {name}"


def resolve_stack(name: str, production_dir: PathLike) -> StackPaths:
    """Resolve a stack name, selecting the sibling's secrets for a ``ci`` run."""

    if name == PRODUCTION_STACK:
        return StackPaths(name, Path(production_dir), None, flag_fragment(name))
    if name == CI_STACK:
        secrets_dir = Path(CI_SECRETS_DIR)
        secrets.select_directory(secrets_dir)
        return StackPaths(name, Path(CI_ROOT), secrets_dir, flag_fragment(name))
    raise ValueError(f"unsupported evaluation stack: {name}")
