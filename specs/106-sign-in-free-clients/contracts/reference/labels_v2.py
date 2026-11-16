"""Flow rules and gate order of astral.labels/v2: the 105 label contract with an outside-service flag on every effect and a refusal of PHI to an unapproved outside service.
Joins, inheritance and quarantined values come unchanged from the preserved 105 labels module, and generate_phi_destination_vectors.py runs every vector through this one.
"""

from __future__ import annotations

from typing import Any

from astral_mesh_ref.errors import MeshError
from astral_mesh_ref.labels import (
    EFFECT_KINDS,
    OUTWARD_KINDS,
    TARGETS,
    derive,
    join,
    quarantined_value,
    running_label,
)

__all__ = ["derive", "flow_rules", "gate_effect", "join", "quarantined_value"]


def _refuse(code: str) -> dict[str, Any]:
    return {"outcome": "refuse", "code": code, "reasons": []}


def _check_effect(effect: Any) -> None:
    if not isinstance(effect, dict):
        raise MeshError("labels.invalid", "effect")
    if effect.get("kind") not in EFFECT_KINDS or effect.get("target") not in TARGETS:
        raise MeshError("labels.invalid", "effect kind or target")
    outside = effect.get("outside_service")
    if type(outside) is not bool:
        raise MeshError("labels.invalid", "outside_service")
    if outside and effect["target"] != "remote_tool":
        raise MeshError("labels.invalid", "an outside service is a remote tool")


def flow_rules(turn: dict[str, Any], effect: dict[str, Any]) -> dict[str, Any]:
    _check_effect(effect)
    running = running_label(turn["context"])
    holds_phi = any(label["confidentiality"] == "phi" for label in turn["context"])
    if effect["target"] == "model":
        if turn["secret_in_context"] or any(
            label["confidentiality"] == "secret" for label in turn["context"]
        ):
            return _refuse("flow.secret_in_context")
        if running["confidentiality"] == "phi" and not effect["endpoint_phi_approved"]:
            return _refuse("flow.phi_to_model")
    if effect["outside_service"] and holds_phi and not effect["endpoint_phi_approved"]:
        return _refuse("flow.phi_to_service")
    if effect["target"] == "sync":
        return {"outcome": "allow", "code": None, "reasons": []}
    reasons: list[str] = []
    untrusted = running["integrity"] == "untrusted"
    if untrusted and effect["kind"] == "outside_effect":
        reasons.append("untrusted_influence")
    if untrusted and turn["private_access"] and effect["kind"] in OUTWARD_KINDS:
        reasons.append("rule_of_two")
    if effect["kind"] == "outside_effect" and any(
        a == "untrusted" for a in effect["argument_integrity"]
    ):
        reasons.append("untrusted_argument")
    return {
        "outcome": "consent" if reasons else "allow",
        "code": None,
        "reasons": reasons,
    }


def _stop(gate: int, code: str) -> dict[str, Any]:
    return {"gate": gate, "outcome": "refuse", "code": code, "reasons": []}


def gate_effect(state: dict[str, Any]) -> dict[str, Any]:
    if state["stopped"]:
        return _stop(1, "gate.mesh_stopped")
    if not state["device_on_roster"]:
        return _stop(1, "gate.device_not_on_roster")
    if state["hard_flag"]:
        return _stop(2, "gate.hard_flag")
    if not state["tool_def_matches"]:
        return _stop(2, "gate.tool_def_changed")
    if not state["permission"]:
        return _stop(3, "gate.permission_denied")
    if state["policy"] == "deny":
        return _stop(4, "gate.policy_denied")
    reasons = ["policy"] if state["policy"] == "consent" else []
    flow = flow_rules(state["turn"], state["effect"])
    if flow["outcome"] == "refuse":
        return _stop(5, flow["code"])
    reasons.extend(flow["reasons"])
    if state["allowance"] == "solo":
        reasons.append("solo_allowance")
    if reasons and not state["consent"]:
        return {
            "gate": 6,
            "outcome": "consent",
            "code": "consent.required",
            "reasons": reasons,
        }
    if state["allowance"] == "exhausted":
        return _stop(7, "allowance.exhausted")
    return {"gate": 8, "outcome": "allow", "code": None, "reasons": reasons}
