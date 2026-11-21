"""Flow labels: the two lattices, inheritance, the running turn label, the flow rules of the kernel and the nine-step gate order for one effect.
Events carry labels from event.py, tickets carry them from ticket.py, and the engine calls gate_effect before dispatch.
"""

from __future__ import annotations

from typing import Any

from .errors import MeshError
from .event import CONFIDENTIALITY, INTEGRITY

OUTWARD_KINDS = ("outside_effect", "outward_read")
EFFECT_KINDS = ("mesh_read", "mesh_write", "outward_read", "outside_effect")
TARGETS = ("local_tool", "remote_tool", "model", "sync")


def join(labels: list[dict[str, str]]) -> dict[str, str]:
    if not labels:
        return {"confidentiality": "public", "integrity": "owner"}
    for label in labels:
        if (
            label.get("integrity") not in INTEGRITY
            or label.get("confidentiality") not in CONFIDENTIALITY
        ):
            raise MeshError("labels.invalid", str(label))
    return {
        "confidentiality": max(
            (label["confidentiality"] for label in labels), key=CONFIDENTIALITY.index
        ),
        "integrity": max((label["integrity"] for label in labels), key=INTEGRITY.index),
    }


def derive(
    sources: list[dict[str, str]], declared: dict[str, str] | None = None
) -> dict[str, str]:
    return join(sources + ([declared] if declared else []))


def running_label(context: list[dict[str, str]]) -> dict[str, str]:
    return join(context)


def quarantined_value(value: Any, schema_ok: bool) -> dict[str, Any]:
    if not schema_ok:
        raise MeshError(
            "labels.quarantine_schema",
            "the reader answer did not match its fixed schema",
        )
    return {"integrity": "untrusted", "value": value, "steers_turn": False}


def flow_rules(turn: dict[str, Any], effect: dict[str, Any]) -> dict[str, Any]:
    if effect["kind"] not in EFFECT_KINDS or effect["target"] not in TARGETS:
        raise MeshError("labels.invalid", "effect kind or target")
    running = running_label(turn["context"])
    reasons: list[str] = []
    untrusted = running["integrity"] == "untrusted"
    outward = effect["kind"] in OUTWARD_KINDS
    if effect["target"] == "model":
        if turn["secret_in_context"] or any(
            label["confidentiality"] == "secret" for label in turn["context"]
        ):
            return {
                "outcome": "refuse",
                "code": "flow.secret_in_context",
                "reasons": [],
            }
        if running["confidentiality"] == "phi" and not effect["endpoint_phi_approved"]:
            return {"outcome": "refuse", "code": "flow.phi_to_model", "reasons": []}
    if effect["target"] == "sync":
        return {"outcome": "allow", "code": None, "reasons": []}
    if untrusted and effect["kind"] == "outside_effect":
        reasons.append("untrusted_influence")
    if untrusted and turn["private_access"] and outward:
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


def gate_effect(state: dict[str, Any]) -> dict[str, Any]:
    if state["stopped"]:
        return {
            "gate": 1,
            "outcome": "refuse",
            "code": "gate.mesh_stopped",
            "reasons": [],
        }
    if not state["device_on_roster"]:
        return {
            "gate": 1,
            "outcome": "refuse",
            "code": "gate.device_not_on_roster",
            "reasons": [],
        }
    if state["hard_flag"]:
        return {"gate": 2, "outcome": "refuse", "code": "gate.hard_flag", "reasons": []}
    if not state["tool_def_matches"]:
        return {
            "gate": 2,
            "outcome": "refuse",
            "code": "gate.tool_def_changed",
            "reasons": [],
        }
    if not state["permission"]:
        return {
            "gate": 3,
            "outcome": "refuse",
            "code": "gate.permission_denied",
            "reasons": [],
        }
    reasons: list[str] = []
    if state["policy"] == "deny":
        return {
            "gate": 4,
            "outcome": "refuse",
            "code": "gate.policy_denied",
            "reasons": [],
        }
    if state["policy"] == "consent":
        reasons.append("policy")
    flow = flow_rules(state["turn"], state["effect"])
    if flow["outcome"] == "refuse":
        return {"gate": 5, "outcome": "refuse", "code": flow["code"], "reasons": []}
    reasons.extend(flow["reasons"])
    allowance = state["allowance"]
    if allowance == "solo":
        reasons.append("solo_allowance")
    if reasons and not state["consent"]:
        return {
            "gate": 6,
            "outcome": "consent",
            "code": "consent.required",
            "reasons": reasons,
        }
    if allowance == "exhausted":
        return {
            "gate": 7,
            "outcome": "refuse",
            "code": "allowance.exhausted",
            "reasons": [],
        }
    return {"gate": 8, "outcome": "allow", "code": None, "reasons": reasons}
