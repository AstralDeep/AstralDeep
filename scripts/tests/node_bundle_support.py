"""Puts the node pack builder and its launcher modules on the import path and re-exports them so the node bundle tests import them by one name."""

import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[1] / "node_bundle"
for entry in (BUNDLE, BUNDLE / "launcher"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import assemble
import astral_node_launch
import manifest
import node_logging
import relocation_check
import verify

__all__ = ["assemble", "astral_node_launch", "manifest", "node_logging", "relocation_check", "verify"]
