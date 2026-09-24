"""Deterministic scripted driver: models each envelope layer's enforcement as blocking
iff the layer is enabled and implemented, so the adapter-to-report pipeline
(drivers/base.py contract) runs reproducibly without a database.
"""

from __future__ import annotations

from security_benchmark.adapters.base import BenchmarkCase, CaseTrace, ToolCallObservation
from security_benchmark.envelope import (
    LAYER_FOR_OBJECTIVE,
    NOT_IMPLEMENTED,
    EnvelopeConfig,
)
from security_benchmark.drivers.base import Driver

_CONTROL_MARKERS = ("benign", "control")


def _is_control(case: BenchmarkCase) -> bool:
    cat = case.category.lower()
    return (not case.injection) or any(m in cat for m in _CONTROL_MARKERS)


class SyntheticDriver(Driver):
    mode = "synthetic"

    def run_case(self, case: BenchmarkCase, envelope: EnvelopeConfig) -> CaseTrace:
        trace = CaseTrace(case_id=case.case_id, envelope_label=envelope.label)

        if case.out_of_corpus:
            trace.notes = "out-of-corpus: no matching Astral capability"
            return trace

        if _is_control(case):
            trace.bait_taken = False
            trace.notes = "control case — agent did not attempt the adversarial action"
            return trace

        trace.bait_taken = True
        obj = case.objective
        responsible = LAYER_FOR_OBJECTIVE.get(obj.kind)
        layer_enabled = bool(responsible) and envelope.is_enabled(responsible)
        layer_implemented = responsible not in NOT_IMPLEMENTED
        blocked = layer_enabled and layer_implemented

        call = ToolCallObservation(
            tool=obj.target_tool or "unknown_tool",
            required_scope=obj.required_scope,
            in_scope=False,
            executed=not blocked,
            blocked_by=responsible if blocked else None,
            egress_fired=(obj.kind == "exfil_egress") and (not blocked),
            phi_crossed_boundary=(obj.kind == "phi_egress") and (not blocked),
            is_adversarial=True,
        )
        trace.tool_calls.append(call)
        trace.notes = (
            f"responsible_layer={responsible} enabled={layer_enabled} "
            f"implemented={layer_implemented} → {'BLOCKED' if blocked else 'EXECUTED'}"
        )
        return trace
