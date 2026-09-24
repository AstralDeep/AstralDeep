"""Structured performance-span logger: perf_span() times a block and emits one `perf
<name> duration_ms=<int> ...` log line even if it raises;
backend/scripts/perf_report.py aggregates these into per-span P50/P95.
"""

import logging
import time
from contextlib import contextmanager

logger = logging.getLogger("astral.perf")


@contextmanager
def perf_span(name, **ctx):
    start = time.monotonic()
    try:
        yield
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        extra = "".join(f" {k}={v}" for k, v in ctx.items())
        logger.info("perf %s duration_ms=%d%s", name, duration_ms, extra)
