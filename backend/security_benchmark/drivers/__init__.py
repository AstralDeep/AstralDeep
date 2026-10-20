"""Resolves the legacy diagnostic ablation drivers used by runner.py.
The signed-turn qualification driver has a separate explicit entry point.
"""

from __future__ import annotations

from security_benchmark.drivers.base import Driver
from security_benchmark.drivers.synthetic import SyntheticDriver


def get_driver(mode: str, run_id: str = "__bench__local", seed: int = 0,
               model: str | None = None) -> Driver:
    if mode == "synthetic":
        return SyntheticDriver()
    if mode in ("in_process", "external"):
        from security_benchmark.drivers.inprocess import InProcessDriver
        return InProcessDriver(run_id=run_id, seed=seed, model=model)
    if mode == "chained_real":
        from security_benchmark.drivers.chained import ChainedDriver
        return ChainedDriver()
    raise ValueError(f"unknown driver mode: {mode!r}")


__all__ = ["Driver", "SyntheticDriver", "get_driver"]
