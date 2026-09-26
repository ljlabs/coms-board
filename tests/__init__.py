"""coms-board test suite (stdlib unittest only).

Run from the skill root:  python3 -m unittest discover -s tests -v
Every test uses a throwaway SQLite file under a TemporaryDirectory; the real
data/coms.db is never touched.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
