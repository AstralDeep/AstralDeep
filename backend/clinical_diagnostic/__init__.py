"""Marks the eval-only clinical answer diagnostic package; the dependency-isolation guard
keeps product runtime from importing it. SUITE_VERSION is stamped into every run record.
"""

from __future__ import annotations

SUITE_VERSION = "0.1.0"

__all__ = ["SUITE_VERSION"]