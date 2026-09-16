"""Run the image-build tool as ``python3 -m tools.imagebuild``."""

import sys

from tools.imagebuild.cli import main

if __name__ == "__main__":
    sys.exit(main())
