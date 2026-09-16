"""Run the redaction command as ``python3 -m tools.redact``."""

import sys

if __name__ == "__main__":
    # Under sudo (a site file the session's user cannot read) bytecode written
    # from here would be root's in the checkout; the flag stops it, and the two
    # package files Python cached before this line are handed back at the end,
    # as the acceptance and turn harnesses arrange.
    sys.dont_write_bytecode = True
    from pathlib import Path

    from gideon.host.sysio import RealHost
    from tools.ownership import restore_bytecode_ownership, sudo_ids
    from tools.redact.cli import main

    try:
        code = main()
    finally:
        # Also after argparse's exit or a failed read: the caches go back whatever happened.
        owner = sudo_ids()
        if owner is not None:
            restore_bytecode_ownership(
                RealHost(), owner, checkout=Path(__file__).resolve().parents[2]
            )
    sys.exit(code)
