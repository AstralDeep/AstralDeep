"""Puts the node pack builder and its launcher modules on the import path for the node bundle tests."""

import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[1] / "node_bundle"
for entry in (BUNDLE, BUNDLE / "launcher"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
