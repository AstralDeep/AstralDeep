"""Contracts for legacy ablation drivers that return one trace per case.
Signed-turn qualification uses the separate real_dispatch driver.
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
