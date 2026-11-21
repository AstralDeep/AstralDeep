"""Puts the spike folder on sys.path so the tests import the adapter and fixture by module name."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
