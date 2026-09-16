"""The command ``python3 -m tools.redact --site <file>``: a transcript, stdin to stdout.

The site file is loaded through the product's own loader, so the derived
values (the search base, the group names) are the ones the box runs with;
its refusals are the loader's, each ending in its fix (slice-1 ticket 55).
"""

import argparse
import sys
from collections.abc import Sequence
from typing import BinaryIO, Final, TextIO

from gideon.host.site import load_site, render_errors
from gideon.host.sysio import Host, RealHost
from tools.redact.core import redact

_ENCODING: Final = "utf-8"
# A transcript's bytes round-trip: an undecodable byte survives the pass.
_ERRORS: Final = "surrogateescape"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.redact",
        description=(
            "Replace every office value the site file declares with its site-key "
            "placeholder, stdin to stdout (slice-1 ticket 55; the tree is public "
            "at 1.0, spec §2.6)."
        ),
    )
    parser.add_argument(
        "--site",
        required=True,
        metavar="PATH",
        help="the site file whose marked leaves name the office values (§3.3)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    stderr: TextIO | None = None,
    host: Host | None = None,
) -> int:
    """Redact stdin against the site file; exit 0, or 1 with the loader's errors."""

    options = build_parser().parse_args(argv)
    loaded = load_site(options.site, host=host or RealHost())
    if loaded.config is None:
        print(render_errors(loaded.errors), file=stderr if stderr is not None else sys.stderr)
        return 1
    source = stdin if stdin is not None else sys.stdin.buffer
    target = stdout if stdout is not None else sys.stdout.buffer
    text = source.read().decode(_ENCODING, errors=_ERRORS)
    target.write(redact(text, loaded.config).encode(_ENCODING, errors=_ERRORS))
    target.flush()
    return 0
