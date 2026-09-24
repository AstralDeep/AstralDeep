"""Defines the ablation axis: independently-toggleable defense layers (scopes/DAF, PHI
gate, red-team, LLM-judge, chained-delegation), each mapped to a real mechanism,
consumed by synthetic.py's model and report.py's breakdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

LAYER_NONE = "none"
LAYER_SCOPES_DAF = "scopes_daf"
LAYER_PHI = "phi_gate"
LAYER_REDTEAM = "redteam"
LAYER_LLM_JUDGE = "llm_judge"
LAYER_CHAINED_DELEGATION = "chained_delegation"

LADDER: List[str] = [LAYER_NONE, LAYER_SCOPES_DAF, LAYER_PHI, LAYER_REDTEAM,
                     LAYER_LLM_JUDGE, LAYER_CHAINED_DELEGATION]

NOT_IMPLEMENTED: frozenset = frozenset({LAYER_LLM_JUDGE})

LAYER_FOR_OBJECTIVE: Dict[str, str] = {
    "forbidden_tool": LAYER_SCOPES_DAF,
    "out_of_scope_action": LAYER_SCOPES_DAF,
    "privilege_escalation": LAYER_SCOPES_DAF,
    "phi_egress": LAYER_PHI,
    "exfil_egress": LAYER_REDTEAM,
    "prompt_injection_semantic": LAYER_LLM_JUDGE,
    "confused_deputy": LAYER_CHAINED_DELEGATION,
    "cross_hop_escalation": LAYER_CHAINED_DELEGATION,
    "depth_violation": LAYER_CHAINED_DELEGATION,
    "actor_chain_forgery": LAYER_CHAINED_DELEGATION,
    "chained_consent_replay": LAYER_CHAINED_DELEGATION,
}


@dataclass(frozen=True)
class EnvelopeConfig:
    scopes_daf: bool = False
    phi_gate: bool = False
    redteam: bool = False
    llm_judge: bool = False
    chained_delegation: bool = False

    @property
    def enabled_layers(self) -> List[str]:
        out = [LAYER_NONE]
        if self.scopes_daf:
            out.append(LAYER_SCOPES_DAF)
        if self.phi_gate:
            out.append(LAYER_PHI)
        if self.redteam:
            out.append(LAYER_REDTEAM)
        if self.llm_judge:
            out.append(LAYER_LLM_JUDGE)
        if self.chained_delegation:
            out.append(LAYER_CHAINED_DELEGATION)
        return out

    def is_enabled(self, layer: str) -> bool:
        if layer == LAYER_NONE:
            return True
        return bool(getattr(self, layer, False))

    @property
    def label(self) -> str:
        if not any((self.scopes_daf, self.phi_gate, self.redteam,
                    self.llm_judge, self.chained_delegation)):
            return "none"
        parts = []
        if self.scopes_daf:
            parts.append("DAF")
        if self.phi_gate:
            parts.append("PHI")
        if self.redteam:
            parts.append("RT")
        if self.llm_judge:
            parts.append("LLM")
        if self.chained_delegation:
            parts.append("CHAIN")
        return "+".join(parts)

    def to_dict(self) -> Dict[str, bool]:
        return {
            "scopes_daf": self.scopes_daf,
            "phi_gate": self.phi_gate,
            "redteam": self.redteam,
            "llm_judge": self.llm_judge,
            "chained_delegation": self.chained_delegation,
        }


def default_ablation_matrix() -> List[EnvelopeConfig]:
    return [
        EnvelopeConfig(),
        EnvelopeConfig(scopes_daf=True),
        EnvelopeConfig(scopes_daf=True, phi_gate=True),
        EnvelopeConfig(scopes_daf=True, phi_gate=True, redteam=True),
        EnvelopeConfig(scopes_daf=True, phi_gate=True, redteam=True, llm_judge=True),
        EnvelopeConfig(scopes_daf=True, phi_gate=True, redteam=True,
                       llm_judge=True, chained_delegation=True),
    ]


def full_envelope() -> EnvelopeConfig:
    return EnvelopeConfig(scopes_daf=True, phi_gate=True, redteam=True,
                          llm_judge=False, chained_delegation=True)


def chaining_off() -> EnvelopeConfig:
    return EnvelopeConfig(scopes_daf=True, phi_gate=True, redteam=True,
                          llm_judge=False, chained_delegation=False)


def chaining_on() -> EnvelopeConfig:
    return EnvelopeConfig(scopes_daf=True, phi_gate=True, redteam=True,
                          llm_judge=False, chained_delegation=True)
