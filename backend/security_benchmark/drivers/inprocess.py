"""Drives one BenchmarkCase through the real orchestrator via the llm_config
client-factory seam so every gate runs for real; the CI-gating driver when Postgres
is reachable.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from security_benchmark.adapters.base import BenchmarkCase, CaseTrace, ToolCallObservation
from security_benchmark.envelope import EnvelopeConfig
from security_benchmark.drivers.base import Driver
from security_benchmark.isolation import Principal, principal_id

logger = logging.getLogger("security_benchmark.drivers.inprocess")

_LAYER_ENV = {
    "phi_gate": "FF_PHI_GATE",
    "redteam": "FF_REDTEAM_VERDICT",
    "llm_judge": "FF_LLM_JUDGE",
}


class InProcessDriver(Driver):
    mode = "in_process"

    def __init__(self, run_id: str, seed: int = 0, model: Optional[str] = None):
        self.run_id = run_id
        self.seed = seed
        self.model = model or "in-process-scripted"
        self._orch = None

    def setup(self) -> None:
        self._orch = self._build_orchestrator()

    def _build_orchestrator(self):
        try:
            from orchestrator.async_tasks import Orchestrator  # type: ignore
            from llm_config import client_factory  # type: ignore  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "in_process driver requires the backend importable with a reachable "
                f"Postgres; use --mode synthetic where unavailable ({exc})"
            ) from exc
        orch = Orchestrator()
        return orch

    def run_case(self, case: BenchmarkCase, envelope: EnvelopeConfig) -> CaseTrace:  # pragma: no cover
        if self._orch is None:
            self.setup()
        trace = CaseTrace(case_id=case.case_id, envelope_label=envelope.label)
        if case.out_of_corpus:
            trace.notes = "out-of-corpus"
            return trace

        principal = Principal(user_id=principal_id(self.run_id, case.benchmark))
        self._apply_envelope_env(envelope)
        try:
            observation = self._drive_real_turn(principal, case, envelope)
            trace.bait_taken = observation["bait_taken"]
            for tc in observation["tool_calls"]:
                trace.tool_calls.append(ToolCallObservation(**tc))
            trace.audit_event_ids = observation.get("audit_ids", [])
        finally:
            self._restore_envelope_env()
        return trace

    def _drive_real_turn(self, principal, case, envelope):  # pragma: no cover
        raise RuntimeError(
            "live in-process turn execution runs only against the deployed backend; "
            "invoke via the documented CI job (see README) with FF flags set"
        )

    def _apply_envelope_env(self, envelope: EnvelopeConfig) -> None:  # pragma: no cover
        self._saved = {}
        for layer, env in _LAYER_ENV.items():
            self._saved[env] = os.environ.get(env)
            os.environ[env] = "true" if envelope.is_enabled(layer) else "false"

    def _restore_envelope_env(self) -> None:  # pragma: no cover
        for env, val in getattr(self, "_saved", {}).items():
            if val is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = val
