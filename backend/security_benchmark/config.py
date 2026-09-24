"""Run configuration for the security-benchmark harness: selects synthetic (scripted,
DB-free), in_process (drives the real orchestrator), or external mode; consumed by
runner.py and __main__.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Literal, Optional

from security_benchmark.envelope import EnvelopeConfig, default_ablation_matrix

Mode = Literal["synthetic", "in_process", "external"]

DEFAULT_ARTIFACTS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_artifacts")


@dataclass
class RunConfig:
    mode: Mode = "synthetic"
    model: str = "scripted-deterministic"
    benchmarks: List[str] = field(default_factory=lambda: ["agentdojo"])
    seed: int = 0
    limit: Optional[int] = None
    ablation: List[EnvelopeConfig] = field(default_factory=default_ablation_matrix)
    artifacts_root: str = DEFAULT_ARTIFACTS_ROOT
    run_id: Optional[str] = None
    asr_threshold: Optional[float] = None

    def normalized_run_id(self, stamp: str = "local") -> str:
        from security_benchmark.isolation import NAMESPACE_PREFIX
        raw = self.run_id or stamp or "local"
        return raw if raw.startswith(NAMESPACE_PREFIX) else f"{NAMESPACE_PREFIX}{raw}"
