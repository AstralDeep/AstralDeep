"""Marks the eval-only security-benchmark harness package; isolation_check.py enforces
that product runtime never imports it. Exposes HARNESS_VERSION, stamped into every
run record.
"""

from __future__ import annotations

HARNESS_VERSION = "0.1.0"

__all__ = ["HARNESS_VERSION"]
