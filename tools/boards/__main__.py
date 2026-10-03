"""Run the board check as ``python -m tools.boards``."""

import sys

if __name__ == "__main__":
    # Root runs the check from a CSA's checkout: bytecode written before the
    # hand-back would remain root-owned in that checkout.
    sys.dont_write_bytecode = True
    from tools.boards.cli import main

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
