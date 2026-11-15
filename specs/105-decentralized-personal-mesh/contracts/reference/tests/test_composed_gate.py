"""Checks every composed-gate fixture case against the reference gate and its own coverage of the labels_gate states.
Composed hosts drive the same cases through real dispatch, so the expectation they assert must first be the pure gate's decision over the case's state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astral_mesh_ref import labels

CONTRACTS = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(
    (CONTRACTS / "conformance/composed_gate.json").read_text(encoding="utf-8")
)
LOOP = json.loads((CONTRACTS / "conformance/loop.json").read_text(encoding="utf-8"))
GATE_STATES = {
    c["name"]: c
    for c in json.loads(
        (CONTRACTS / "vectors/labels.json").read_text(encoding="utf-8")
    )["cases"]
    if c["kind"] == "labels_gate"
}
CASES = FIXTURE["cases"]
SETUP_OPS = set(FIXTURE["operations"])
TIMINGS = set(FIXTURE["timing"])
CONSENTS = set(FIXTURE["consent"])
EXPECT_KEYS = set(FIXTURE["expect"]) | {"outcome", "code", "reasons"}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_expectation_is_the_pure_gate_decision(case: dict) -> None:
    expect = case["expect"]
    assert labels.gate_effect(case["state"]) == {
        "gate": expect["gate"],
        "outcome": expect["outcome"],
        "code": expect["code"],
        "reasons": expect["reasons"],
    }


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_case_is_well_formed(case: dict) -> None:
    assert case["source"] in GATE_STATES
    assert case["consent"] in CONSENTS
    assert set(case["expect"]) == EXPECT_KEYS
    for op in case["setup"]:
        assert op["op"] in SETUP_OPS
        assert op.get("at", "before_turn") in TIMINGS
    for host, reason in case.get("skip", {}).items():
        assert host and reason.strip()
    expect = case["expect"]
    if expect["outcome"] == "allow":
        assert expect["dispatched"] == 1 and expect["journal_status"] == "ok"
    else:
        assert expect["dispatched"] == 0
    if expect["outcome"] == "consent":
        assert expect["prompt"] == {"reasons": expect["reasons"]}
    if any(op.get("at") == "awaiting_consent" for op in case["setup"]):
        assert case["consent"] == "valid" and expect["prompt"] is not None


def test_every_gate_state_has_a_composed_case() -> None:
    assert set(GATE_STATES) <= {c["source"] for c in CASES}


def test_every_precedence_collision_runs_or_names_its_skip() -> None:
    order = [n for n in GATE_STATES if n.startswith("order-")]
    assert len(order) == 6
    for name in order:
        assert any(c["source"] == name for c in CASES)


def test_names_are_unique() -> None:
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names))


def test_loop_fixture_points_at_this_file() -> None:
    assert LOOP["composed_gate"] == "composed_gate.json"
    assert FIXTURE["gate_vectors"] == LOOP["gate_vectors"]
