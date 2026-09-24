"""Contract for a driver that executes one BenchmarkCase under one EnvelopeConfig and
returns a CaseTrace; synthetic.py models enforcement deterministically while
inprocess.py and chained.py drive the real gates.
"""

from __future__ import annotations

import abc

from security_benchmark.adapters.base import BenchmarkCase, CaseTrace
from security_benchmark.envelope import EnvelopeConfig


class Driver(abc.ABC):
    mode: str = ""

    @abc.abstractmethod
    def run_case(self, case: BenchmarkCase, envelope: EnvelopeConfig) -> CaseTrace:
        raise NotImplementedError

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass
