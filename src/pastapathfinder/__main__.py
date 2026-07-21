"""Support `python -m pastapathfinder`, equivalent to the console script (design.md §3.1)."""

import sys

from pastapathfinder.cli import main

if __name__ == "__main__":
    sys.exit(main())
