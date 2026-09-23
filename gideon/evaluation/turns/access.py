"""Resolve the instruction, eval identity, and frontend client for turn runs."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from gideon.host import owui, secrets, tls
from gideon.host.render import command as render_command
from gideon.host.render.ci import CI_BASE_URL, CI_SECRET_NAMES
from gideon.host.render.owui import EVAL_PASSWORD_SECRET, general_texts
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

_PASSWORD_FIX: Final[str] = "Run sudo python3 -m gideon apply, then retry."
_RENDER_INPUTS_FIX: Final[str] = "Correct the checkout's render inputs, then retry."


@dataclass(frozen=True, slots=True)
class TurnAccess:
    """The values shared by a suite's service and managed frontend turns."""

    instruction: str
    password: str = field(repr=False)
    client_factory: Callable[..., owui.Client]
    sentinel: str


def load_general_instruction(
    io: Host,
    *,
    site_path: PathLike,
    root: Path,
    stack: str,
    command: str,
) -> str | Problem:
    """Load General's rendered instruction, or return the caller's refusal row.

    ``command`` names the caller in the render inputs' own refusals on stderr.
    """

    inputs = render_command.load_render_inputs(
        io,
        site_path=site_path,
        lock_path=root / "host.lock",
        images_path=root / "images.lock",
        models_path=root / "models.lock",
        root=root,
        command=command,
        secret_names=CI_SECRET_NAMES if stack == "ci" else None,
    )
    if inputs is None:
        return Problem("render inputs are unavailable", _RENDER_INPUTS_FIX)
    try:
        return general_texts(inputs[0]).system_prompt
    except (TypeError, ValueError) as exc:
        return Problem(f"General instruction is unavailable: {exc}", _RENDER_INPUTS_FIX)


def read_eval_password(io: Host) -> str | Problem:
    """Read the eval identity password, or return the CLI's refusal row."""

    result = secrets.read_secret(io, EVAL_PASSWORD_SECRET)
    if not result.ok or result.value is None:
        fix = _PASSWORD_FIX if result.missing else result.fix or _PASSWORD_FIX
        return Problem(
            result.problem or "the evaluation password is unavailable",
            fix,
        )
    return result.value


def make_client_factory(
    hostname: str,
    *,
    stack: str,
    timeout: float,
) -> Callable[..., owui.Client]:
    """Build the frontend client factory for production or the CI sibling."""

    if stack == "ci":
        return owui.loopback_client_factory(CI_BASE_URL, timeout=timeout)
    return owui.ingress_client_factory(hostname, ca_path=tls.CA_PATH, timeout=timeout)
