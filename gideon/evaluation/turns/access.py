"""Resolve turn access and the unfiltered driver's rendered instruction."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from gideon.host import owui, report, secrets, tls
from gideon.host.render import command as render_command
from gideon.host.render.ci import CI_BASE_URL, CI_SECRET_NAMES, CI_STACK
from gideon.host.render.owui import EVAL_PASSWORD_SECRET, general_texts
from gideon.host.report import Problem
from gideon.host.sysio import Host, PathLike

_RENDER_INPUTS_FIX: Final[str] = "Correct the checkout's render inputs, then retry."


def _password_fix() -> str:
    return f"Run {report.command('apply')}, then retry."


@dataclass(frozen=True, slots=True)
class TurnAccess:
    """The password, client factory, and sentinel for a suite's turns."""

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
        secret_names=CI_SECRET_NAMES if stack == CI_STACK else None,
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
        fix = _password_fix() if result.missing else result.fix or _password_fix()
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

    if stack == CI_STACK:
        return owui.loopback_client_factory(CI_BASE_URL, timeout=timeout)
    return owui.ingress_client_factory(hostname, ca_path=tls.CA_PATH, timeout=timeout)
