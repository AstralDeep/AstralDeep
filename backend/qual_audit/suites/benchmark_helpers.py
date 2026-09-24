"""Timing and statistics helpers (BenchmarkResult with a 95% confidence interval, a
context-manager Timer) shared by qual_audit/suites/test_transport_comparison.py's
latency benchmarks.
"""

import time
from dataclasses import dataclass, field
from typing import List

import numpy as np


@dataclass
class BenchmarkResult:
    transport: str
    sample_count: int
    latencies_ms: List[float] = field(default_factory=list)

    @property
    def mean(self) -> float:
        return float(np.mean(self.latencies_ms)) if self.latencies_ms else 0.0

    @property
    def median(self) -> float:
        return float(np.median(self.latencies_ms)) if self.latencies_ms else 0.0

    @property
    def p95(self) -> float:
        return float(np.percentile(self.latencies_ms, 95)) if self.latencies_ms else 0.0

    @property
    def p99(self) -> float:
        return float(np.percentile(self.latencies_ms, 99)) if self.latencies_ms else 0.0

    @property
    def stddev(self) -> float:
        return float(np.std(self.latencies_ms)) if self.latencies_ms else 0.0

    def to_dict(self) -> dict:
        return {
            "transport": self.transport,
            "sample_count": self.sample_count,
            "mean_ms": round(self.mean, 3),
            "median_ms": round(self.median, 3),
            "p95_ms": round(self.p95, 3),
            "p99_ms": round(self.p99, 3),
            "stddev_ms": round(self.stddev, 3),
        }

    def confidence_interval_95(self) -> tuple:
        if len(self.latencies_ms) < 2:
            return (self.mean, self.mean)
        from scipy import stats
        ci = stats.t.interval(
            0.95,
            df=len(self.latencies_ms) - 1,
            loc=self.mean,
            scale=stats.sem(self.latencies_ms),
        )
        return (round(ci[0], 3), round(ci[1], 3))


class Timer:
    def __init__(self):
        self.elapsed_ms: float = 0.0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000
