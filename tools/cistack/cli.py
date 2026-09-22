"""Parse and dispatch the CI sibling stack's three operator commands."""

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from gideon.host import owui
from gideon.host.render.ci import CI_BASE_URL, CI_ROOT, CI_SECRETS_DIR
from gideon.host.sysio import Host, PathLike, RealHost
from tools.cistack import run

DEFAULT_SITE_PATH: Final[Path] = Path("/etc/gideon/site.yaml")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tools.cistack")
    commands = parser.add_subparsers(dest="command", required=True)

    up = commands.add_parser("up")
    up.add_argument("--checkout", type=Path, metavar="PATH")

    down = commands.add_parser("down")
    down.add_argument("--wipe", action="store_true")

    commands.add_parser("status")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    client_factory: Callable[..., owui.Client] | None = None,
    checkout: Path | None = None,
    site_path: PathLike = DEFAULT_SITE_PATH,
) -> int:
    """Parse one CI stack command and run it through the host seam."""

    options = _parser().parse_args(argv)
    io = host or RealHost()
    tree = checkout or Path(__file__).resolve().parents[2]
    if options.command == "up" and options.checkout is not None:
        # Compose resolves a relative mount source against the project
        # directory, /data/ci, so the checkout is made absolute here.
        tree = options.checkout.resolve()
    ci_stack = run.CiStack(
        project_dir=Path(CI_ROOT),
        secrets_dir=Path(CI_SECRETS_DIR),
        base_url=CI_BASE_URL,
        checkout=tree,
    )
    if options.command == "up":
        return run.up(ci_stack, io, site_path=site_path, client_factory=client_factory)
    if options.command == "down":
        return run.down(ci_stack, io, wipe=options.wipe)
    return run.status(ci_stack, io)
