"""Runs every caller ticket vector case through the reference and pins the expected outcome of each named case.
A failure means the contract document, the committed vectors and the reference module disagree.
"""

import json
from pathlib import Path

import caller_ticket
import pytest

VECTORS = Path(__file__).resolve().parents[2] / "vectors" / "caller-v1.json"
DOC = json.loads(VECTORS.read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in DOC["cases"]}

VALID = {"covered_tool": "echo"}
EXPECTED = {
    "valid_mcp_tool": VALID,
    "valid_a2a_message": {"covered_tool": "a2a.message"},
    "other_tool_refused": "caller.tool_not_covered",
    "changed_definition_refused": "caller.tool_changed",
    "a2a_not_covered_refused": "caller.tool_not_covered",
    "expired": "caller.expired",
    "lifetime_31_days": "caller.malformed",
    "lifetime_30_days_accepted": VALID,
    "future_issued_ms": "caller.not_yet_valid",
    "bad_presence_signature": "caller.bad_signature",
    "wrong_mesh": "caller.mesh_mismatch",
    "wrong_node": "caller.wrong_node",
    "removed_device": "caller.device_removed",
    "raised_stop": "caller.stopped",
    "revoked": "caller.revoked",
    "proof_method_mismatch": "caller.proof_mismatch",
    "proof_target_mismatch": "caller.proof_mismatch",
    "proof_body_mismatch": "caller.proof_mismatch",
    "proof_ticket_mismatch": "caller.proof_mismatch",
    "stale_proof": "caller.proof_stale",
    "proof_before_verifier_start": "caller.proof_stale",
    "bad_holder_signature": "caller.proof_bad_signature",
    "replayed_nonce": ["ok", "caller.replayed"],
    "unknown_ticket_field": "caller.malformed",
    "unknown_proof_field": "caller.proof_malformed",
    "header_too_long": "caller.malformed",
    "unsorted_tools": "caller.malformed",
    "wrong_scheme": "caller.malformed",
    "missing_proof": "caller.proof_malformed",
    "first_failure_wins": "caller.mesh_mismatch",
    "missing_catalog_tool_refused": "caller.tool_changed",
    "caller_revoke_fold": "fold",
}


def test_document_names_the_contract() -> None:
    assert DOC["format"] == "astral.vectors/v1"
    assert DOC["name"] == "caller-v1"
    assert DOC["spec"] == "106/contracts/caller-ticket.md"


def test_vectors_cover_exactly_the_named_cases() -> None:
    assert set(CASES) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_case_reproduces_its_committed_expectation(name: str) -> None:
    case = CASES[name]
    assert caller_ticket.run(case["kind"], case["input"]) == case["expect"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_case_has_the_contracted_outcome(name: str) -> None:
    want = EXPECTED[name]
    expect = CASES[name]["expect"]
    if want == "fold":
        assert expect["ok"]["ordinary_grant"] == "ok"
        assert expect["ok"]["revoked"] == [CASES[name]["input"]["caller"]]
    elif isinstance(want, list):
        got = [s.get("error", "ok") for s in expect["ok"]["results"]]
        assert got == want
    elif isinstance(want, dict):
        assert expect["ok"]["tool"] == want["covered_tool"]
    else:
        assert expect == {"error": want}


def test_every_error_code_in_the_contract_is_exercised() -> None:
    seen = {
        v["error"]
        for c in DOC["cases"]
        for v in ([c["expect"]] + c["expect"].get("ok", {}).get("results", []))
        if isinstance(v, dict) and "error" in v
    }
    assert (
        set(caller_ticket.CODES) - {"caller.tool_unavailable", "caller.no_catalog"}
        <= seen
    )


def test_vector_inputs_hold_only_public_test_keys() -> None:
    text = VECTORS.read_text(encoding="utf-8")
    assert "scalar" not in text and "seed_hex" not in text


def test_generator_reproduces_the_committed_file() -> None:
    import generate_caller_vectors

    assert generate_caller_vectors.render() == VECTORS.read_text(encoding="utf-8")


def test_ordinary_authority_still_passes_beside_a_caller_revoke() -> None:
    out = caller_ticket.revoke_fold(
        {
            "caller": "AAAAAAAAAAAAAAAAAAAAAA",
            "grants": ["*/echo/*"],
            "revokes": ["caller:AAAAAAAAAAAAAAAAAAAAAA/*/*", "other/echo/mesh_read"],
            "action": {
                "agent": "agent-a",
                "tool": "echo",
                "tool_def": "sha256:" + "0" * 64,
            },
            "effect_kind": "mesh_read",
        }
    )
    assert out["ordinary_grant"] == "ok"
    assert out["revoked"] == ["AAAAAAAAAAAAAAAAAAAAAA"]


def test_lifetime_choices_are_the_contracted_set() -> None:
    assert caller_ticket.LIFETIMES == {
        "1h": 3600,
        "1d": 86400,
        "7d": 604800,
        "30d": 2592000,
    }
    assert caller_ticket.DEFAULT_LIFETIME == "7d"
