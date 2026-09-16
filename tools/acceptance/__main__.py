"""Run the acceptance harness as ``python3 -m tools.acceptance``."""

import sys

if __name__ == "__main__":
    # Root runs this from a CSA's checkout or the runner's workspace: bytecode
    # written from here would be root's, and a root-owned cache breaks the next
    # git clean there (the v0.1.0 tag run poisoned the runner's workspace). The
    # two package files Python cached before this line are handed back with
    # the transcripts at the end of the run.
    sys.dont_write_bytecode = True
    from tools.acceptance.cli import main

    # Rows reach a pipe (a tee transcript, a CI log) as they happen, as the
    # product's own entry point arranges; a 45-minute run must not be silent.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
