"""Benchmark adapter registry mapping a name to its BenchmarkAdapter (agentdojo, asb,
chained, injecagent); runner.py resolves adapters via get_adapter() so adding a
benchmark never touches the core.
"""

from __future__ import annotations

from typing import Dict, Type

from security_benchmark.adapters.agentdojo import AgentDojoAdapter
from security_benchmark.adapters.asb import ASBAdapter
from security_benchmark.adapters.base import BenchmarkAdapter
from security_benchmark.adapters.chained import ChainedAttackAdapter
from security_benchmark.adapters.injecagent import InjecAgentAdapter

REGISTRY: Dict[str, Type[BenchmarkAdapter]] = {
    AgentDojoAdapter.name: AgentDojoAdapter,
    ASBAdapter.name: ASBAdapter,
    InjecAgentAdapter.name: InjecAgentAdapter,
    ChainedAttackAdapter.name: ChainedAttackAdapter,
}


def get_adapter(name: str) -> BenchmarkAdapter:
    try:
        return REGISTRY[name]()
    except KeyError:
        raise KeyError(
            f"unknown benchmark {name!r}; known: {sorted(REGISTRY)}"
        ) from None


__all__ = ["REGISTRY", "get_adapter", "BenchmarkAdapter"]
