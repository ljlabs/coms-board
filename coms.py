#!/usr/bin/env python3
"""Entry point: python3 coms.py --as <agent> <group> <cmd> ..."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coms.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
