"""``python -m builder ...`` entry point (spec R-90)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
