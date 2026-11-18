"""Refuses the retired benchmark driver whose turn execution was never implemented.
"""

from __future__ import annotations

from security_benchmark.adapters.base import BenchmarkCase, CaseTrace
from security_benchmark.drivers.base import Driver
from security_benchmark.envelope import EnvelopeConfig


class InProcessDriver(Driver):
    mode = "in_process"

    def __init__(self, run_id: str, seed: int = 0, model: str | None = None):
        self.run_id = run_id
        self.seed = seed
        self.model = model

    def setup(self) -> None:
        raise RuntimeError(
            "the legacy in_process ASR driver has no real turn executor")

    def run_case(self, case: BenchmarkCase, envelope: EnvelopeConfig) -> CaseTrace:
        self.setup()
        raise AssertionError("unreachable")
