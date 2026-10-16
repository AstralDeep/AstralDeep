"""Marks the eval-only answer-behavior package; isolation_check.py enforces that product
runtime never imports it. Exposes SUITE_VERSION, stamped into every run record.
"""

from __future__ import annotations

SUITE_VERSION = "0.1.0"

__all__ = ["SUITE_VERSION"]
