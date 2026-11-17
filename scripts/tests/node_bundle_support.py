"""Puts the node pack builder and its launcher modules on the import path and re-exports them so the node bundle tests import them by one name."""

import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[1] / "node_bundle"
for entry in (BUNDLE, BUNDLE / "launcher", BUNDLE / "natives"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import assemble  # noqa: E402
import astral_node_launch  # noqa: E402
import build_macos  # noqa: E402
import macho  # noqa: E402
import manifest  # noqa: E402
import node_logging  # noqa: E402
import relocation_check  # noqa: E402
import verify  # noqa: E402

__all__ = ["assemble", "astral_node_launch", "build_macos", "macho", "manifest", "node_logging", "relocation_check",
           "verify"]
