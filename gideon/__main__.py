import sys

from gideon.cli import main


def line_buffered() -> None:
    """Flush stdout at every line: the rows of an ordered command reach a pipe
    (an operator's ``tee`` transcript, a unit's journal) as they happen, and never
    land after the stderr refusal that ended the run."""

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)


if __name__ == "__main__":
    line_buffered()
    sys.exit(main())
