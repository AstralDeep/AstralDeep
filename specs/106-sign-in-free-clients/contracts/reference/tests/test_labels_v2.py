"""Runs every label and gate vector of astral.labels/v2 through the reference and pins the new outside-service rule.
It proves every case of the 105 label vectors is carried over with its outcome unchanged once the effect states it reaches no outside service.
A failure means labels-v2.md, the committed vectors and labels_v2.py disagree."""

import copy
import json
from pathlib import Path

import labels_v2
import pytest
from astral_mesh_ref.errors import MeshError

CONTRACTS = Path(__file__).resolve().parents[2]
DOC = json.loads((CONTRACTS / "vectors/labels-v2.json").read_text(encoding="utf-8"))
V1 = json.loads(
    (
        CONTRACTS.parents[1] / "105-decentralized-personal-mesh/contracts/vectors/labels.json"
    ).read_text(encoding="utf-8")
)
CASES = {case["name"]: case for case in DOC["cases"]}
TEXT = (CONTRACTS / "labels-v2.md").read_text(encoding="utf-8")

REFUSE = {"outcome": "refuse", "code": "flow.phi_to_service", "reasons": []}
ALLOW = {"outcome": "allow", "code": None, "reasons": []}

NEW = {
    "flow-phi-to-an-unapproved-outside-service": {"ok": REFUSE},
    "flow-phi-to-an-approved-outside-service": {"ok": ALLOW},
    "flow-phi-and-secret-to-an-unapproved-outside-service": {"ok": REFUSE},
    "flow-secret-only-to-an-outside-service-keeps-its-v1-outcome": {"ok": ALLOW},
    "flow-private-to-an-unapproved-outside-service": {"ok": ALLOW},
    "flow-phi-to-a-peer-tool": {"ok": ALLOW},
    "flow-phi-to-a-local-tool": {"ok": ALLOW},
    "flow-phi-read-from-an-unapproved-outside-service": {"ok": REFUSE},
    "flow-service-refusal-comes-before-consent": {"ok": REFUSE},
    "flow-approved-service-still-asks-for-consent": {
        "ok": {"outcome": "consent", "code": None, "reasons": ["untrusted_influence"]}
    },
    "flow-outside-service-on-a-model-target": {"error": "labels.invalid"},
    "flow-outside-service-on-a-local-tool": {"error": "labels.invalid"},
    "flow-outside-service-on-sync": {"error": "labels.invalid"},
    "flow-outside-service-missing": {"error": "labels.invalid"},
    "flow-outside-service-not-a-boolean": {"error": "labels.invalid"},
    "flow-phi-to-model-is-unchanged-for-services": {
        "ok": {"outcome": "refuse", "code": "flow.phi_to_model", "reasons": []}
    },
    "gate-5-phi-to-service": {
        "ok": {"gate": 5, "outcome": "refuse", "code": "flow.phi_to_service", "reasons": []}
    },
    "gate-5-approved-service-dispatches": {
        "ok": {"gate": 8, "outcome": "allow", "code": None, "reasons": []}
    },
    "order-policy-before-service-flow": {
        "ok": {"gate": 4, "outcome": "refuse", "code": "gate.policy_denied", "reasons": []}
    },
}


def run(case):
    try:
        if case["kind"] == "labels_flow":
            return {"ok": labels_v2.flow_rules(case["input"]["turn"], case["input"]["effect"])}
        if case["kind"] == "labels_gate":
            return {"ok": labels_v2.gate_effect(case["input"]["state"])}
        if case["kind"] == "labels_join":
            return {"ok": labels_v2.join(case["input"]["labels"])}
        if case["kind"] == "labels_quarantine":
            return {"ok": labels_v2.quarantined_value(**case["input"])}
    except MeshError as exc:
        return {"error": exc.code}
    raise AssertionError(case["kind"])


@pytest.mark.parametrize("name", sorted(CASES))
def test_vector(name):
    assert run(CASES[name]) == CASES[name]["expect"]


def with_service_flag(case):
    case = copy.deepcopy(case)
    if case["kind"] == "labels_flow":
        case["input"]["effect"]["outside_service"] = False
    if case["kind"] == "labels_gate":
        case["input"]["state"]["effect"]["outside_service"] = False
    case["name"] = "v1-" + case["name"]
    return case


@pytest.mark.parametrize("name", [case["name"] for case in V1["cases"]])
def test_every_v1_case_is_carried_over_unchanged(name):
    old = next(case for case in V1["cases"] if case["name"] == name)
    assert CASES["v1-" + name] == with_service_flag(old)


def test_new_cases_are_pinned():
    new = {n for n in CASES if not n.startswith("v1-")}
    assert new == set(NEW)
    for name, want in NEW.items():
        assert CASES[name]["expect"] == want, name


def test_the_service_flag_matters_only_for_phi_to_an_unapproved_service():
    for case in DOC["cases"]:
        if case["kind"] != "labels_flow" or case["expect"].get("error"):
            continue
        turn, effect = case["input"]["turn"], case["input"]["effect"]
        if effect["target"] != "remote_tool":
            continue
        flipped = dict(effect, outside_service=not effect["outside_service"])
        phi = any(label["confidentiality"] == "phi" for label in turn["context"])
        differs = labels_v2.flow_rules(turn, flipped) != labels_v2.flow_rules(turn, effect)
        assert differs == (phi and not effect["endpoint_phi_approved"]), case["name"]


def test_document_names_every_new_case_and_code():
    assert "`flow.phi_to_service`" in TEXT
    assert "`outside_service`" in TEXT
    for name in NEW:
        assert f"`{name}`" in TEXT
