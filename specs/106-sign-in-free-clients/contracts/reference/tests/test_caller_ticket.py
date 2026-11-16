"""Runs every caller ticket vector case through the reference and pins the expected outcome of each named case.
A failure means the contract document, the committed vectors and the reference module disagree.
"""

import itertools
import json
from pathlib import Path

import caller_ticket
import pytest
from astral_mesh_ref import crypto, testkit

VECTORS = Path(__file__).resolve().parents[2] / "vectors" / "caller-v1.json"
DOC = json.loads(VECTORS.read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in DOC["cases"]}

EXPECTED = {
    "valid_mcp_tool": "ok",
    "valid_a2a_message": "ok",
    "pocket_minter_accepted": "ok",
    "satellite_minter_accepted": "ok",
    "other_tool_refused": "caller.tool_not_covered",
    "changed_definition_refused": "caller.tool_changed",
    "missing_catalog_tool_refused": "caller.tool_changed",
    "a2a_not_covered_refused": "caller.tool_not_covered",
    "tools_list_returns_covered_tools": "ok",
    "tools_list_skips_tool_checks": "ok",
    "tools_list_still_checks_stop": "caller.stopped",
    "expired": "caller.expired",
    "expiry_exactly_now": "caller.expired",
    "expiry_one_ms_after_now": "ok",
    "lifetime_31_days": "caller.malformed",
    "lifetime_30_days_accepted": "ok",
    "lifetime_30_days_plus_1_ms": "caller.malformed",
    "expires_equal_to_issued": "caller.malformed",
    "expires_before_issued": "caller.malformed",
    "future_issued_ms": "caller.not_yet_valid",
    "future_issued_ms_at_limit": "ok",
    "bad_presence_signature": "caller.bad_signature",
    "high_s_presence_signature": "caller.bad_signature",
    "wrong_mesh": "caller.mesh_mismatch",
    "wrong_node": "caller.wrong_node",
    "removed_device": "caller.device_removed",
    "unknown_minting_device": "caller.device_removed",
    "node_minter_refused": "caller.wrong_minter",
    "raised_stop": "caller.stopped",
    "revoked": "caller.revoked",
    "revoke_for_one_tool_ignored": "ok",
    "revoke_for_one_scope_ignored": "ok",
    "revoke_of_another_caller_ignored": "ok",
    "later_grant_does_not_restore_revoked": "caller.revoked",
    "lowercase_scheme_refused": "caller.malformed",
    "tool_name_64_accepted": "ok",
    "method_16_characters_accepted": "ok",
    "target_2048_characters_accepted": "ok",
    "proof_method_mismatch": "caller.proof_mismatch",
    "proof_target_mismatch": "caller.proof_mismatch",
    "proof_body_mismatch": "caller.proof_mismatch",
    "proof_ticket_mismatch": "caller.proof_mismatch",
    "stale_proof": "caller.proof_stale",
    "proof_at_window_start": "ok",
    "proof_ahead_5s": "ok",
    "proof_ahead_beyond_5s": "caller.proof_stale",
    "proof_just_after_start_pause_accepted": "ok",
    "proof_at_start_pause_end_refused": "caller.proof_stale",
    "bad_holder_signature": "caller.proof_bad_signature",
    "replayed_nonce": ["ok", "caller.replayed"],
    "same_nonce_on_another_ticket_accepted": ["ok", "ok"],
    "restart_refuses_proof_accepted_before": ["ok", "caller.proof_stale"],
    "replay_store_keeps_a_nonce_to_the_end_of_its_window": [
        "ok",
        "caller.replayed",
        "caller.proof_stale",
    ],
    "replay_key_ignores_the_target": ["ok", "caller.replayed"],
    "replay_key_ignores_the_proof_time": ["ok", "caller.replayed"],
    "restart_refuses_future_proof_accepted_before": ["ok", "caller.proof_stale"],
    "restart_at_proof_time_minus_5s_refuses": ["ok", "caller.proof_stale"],
    "restart_just_before_proof_time_minus_5s_accepts": ["ok", "ok"],
    "restart_keeps_earlier_state_patches": [
        "caller.tool_changed",
        "caller.tool_changed",
    ],
    "failed_signature_does_not_use_the_nonce": ["caller.proof_bad_signature", "ok"],
    "uncovered_tool_uses_the_nonce": ["caller.tool_not_covered", "caller.replayed"],
    "order_bad_holder_before_replayed": ["ok", "caller.proof_bad_signature"],
    "order_replayed_before_tool_not_covered": ["ok", "caller.replayed"],
    "order_replayed_before_tool_changed": ["ok", "caller.replayed"],
    "order_malformed_before_proof_malformed": "caller.malformed",
    "order_proof_malformed_before_mesh": "caller.proof_malformed",
    "order_mesh_before_node": "caller.mesh_mismatch",
    "order_node_before_removed": "caller.wrong_node",
    "order_removed_before_minter": "caller.device_removed",
    "order_minter_before_bad_signature": "caller.wrong_minter",
    "order_bad_signature_before_stopped": "caller.bad_signature",
    "order_stopped_before_expired": "caller.stopped",
    "order_stopped_before_not_yet_valid": "caller.stopped",
    "order_expired_before_revoked": "caller.expired",
    "order_not_yet_valid_before_revoked": "caller.not_yet_valid",
    "order_revoked_before_proof_mismatch": "caller.revoked",
    "order_proof_mismatch_before_proof_stale": "caller.proof_mismatch",
    "order_proof_stale_before_bad_holder": "caller.proof_stale",
    "unknown_ticket_field": "caller.malformed",
    "unknown_proof_field": "caller.proof_malformed",
    "ticket_version_2": "caller.malformed",
    "ticket_version_true": "caller.malformed",
    "empty_label": "caller.malformed",
    "label_64_code_points_accepted": "ok",
    "label_65_code_points": "caller.malformed",
    "label_control_character": "caller.malformed",
    "label_format_character": "caller.malformed",
    "no_tools": "caller.malformed",
    "tools_32_accepted": "ok",
    "tools_33": "caller.malformed",
    "duplicate_tools": "caller.malformed",
    "unsorted_tools": "caller.malformed",
    "tool_name_starts_with_digit": "caller.malformed",
    "tool_name_too_long": "caller.malformed",
    "tool_name_with_dot": "caller.malformed",
    "tool_without_definition": "caller.malformed",
    "a2a_with_definition": "caller.malformed",
    "tool_with_extra_field": "caller.malformed",
    "holder_key_31_bytes": "caller.malformed",
    "header_too_long": "caller.malformed",
    "proof_header_too_long": "caller.proof_malformed",
    "wrong_scheme": "caller.malformed",
    "bearer_scheme_with_valid_ticket": "caller.malformed",
    "ticket_not_canonical_spacing": "caller.malformed",
    "ticket_not_canonical_key_order": "caller.malformed",
    "ticket_not_base64": "caller.malformed",
    "ticket_not_utf8": "caller.malformed",
    "ticket_not_an_object": "caller.malformed",
    "proof_not_canonical": "caller.proof_malformed",
    "proof_not_base64": "caller.proof_malformed",
    "proof_not_utf8": "caller.proof_malformed",
    "proof_method_17_characters": "caller.proof_malformed",
    "proof_target_2049_characters": "caller.proof_malformed",
    "proof_target_without_slash": "caller.proof_malformed",
    "proof_version_2": "caller.proof_malformed",
    "missing_proof": "caller.proof_malformed",
    "first_failure_wins": "caller.mesh_mismatch",
    "caller_revoke_fold": "ok",
    "caller_revoke_fold_control_refuses_covered_agent": "ticket.grant_revoked",
    "caller_revoke_fold_later_grant_keeps_revoke": "ok",
    "ticket_form_accepted": "ok",
    "ticket_form_wrong_prefix": "caller.malformed",
    "ticket_form_wrong_version": "caller.malformed",
    "ticket_form_not_canonical": "caller.malformed",
    "ticket_form_over_limit": "caller.malformed",
    "holder_form_accepted": "ok",
    "key_form_refused_by_minting": "caller.malformed",
    "holder_form_31_bytes": "caller.malformed",
    "holder_form_padded": "caller.malformed",
    "holder_form_wrong_version": "caller.malformed",
    "key_form_derives_holder": "ok",
    "key_form_31_bytes": "caller.malformed",
    "holder_form_refused_as_key": "caller.malformed",
}


def outcome(case: dict) -> object:
    expect = case["expect"]
    if case["kind"] == "caller_verify_sequence":
        return [r.get("error", "ok") for r in expect["ok"]["results"]]
    return expect.get("error", "ok")


def test_document_names_the_contract() -> None:
    assert DOC["format"] == "astral.vectors/v1"
    assert DOC["name"] == "caller-v1"
    assert DOC["spec"] == "106/contracts/caller-ticket.md"


def test_vectors_cover_exactly_the_named_cases() -> None:
    assert set(CASES) == set(EXPECTED)
    assert len(DOC["cases"]) == len(CASES)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_case_reproduces_its_committed_expectation(name: str) -> None:
    case = CASES[name]
    assert caller_ticket.run(case["kind"], case["input"]) == case["expect"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_case_has_the_contracted_outcome(name: str) -> None:
    assert outcome(CASES[name]) == EXPECTED[name]


def test_every_verifier_code_is_exercised() -> None:
    seen = set()
    for want in EXPECTED.values():
        seen.update(want if isinstance(want, list) else [want])
    wanted = set(caller_ticket.CODES) - {"caller.tool_unavailable", "caller.no_catalog"}
    assert wanted <= seen


def test_adjacent_steps_are_pinned_in_the_verification_order() -> None:
    order = [
        "malformed", "proof_malformed", "mesh", "node", "removed", "minter",
        "bad_signature", "stopped", "expired", "revoked", "proof_mismatch",
        "proof_stale", "bad_holder",
    ]  # fmt: skip
    for first, second in itertools.pairwise(order):
        assert f"order_{first}_before_{second}" in CASES, (first, second)
    assert "order_not_yet_valid_before_revoked" in CASES


def test_published_test_keys_are_the_public_labels() -> None:
    keys = DOC["test_keys"]
    assert keys["holder_seed_hex"] == testkit.seed("caller-v1/holder").hex()
    assert keys["stranger_seed_hex"] == testkit.seed("caller-v1/stranger").hex()
    for name, hex_value in keys["presence_scalar_hex"].items():
        assert int(hex_value, 16) == testkit.scalar(f"local-v2/{name}/presence")


def test_published_seed_recomputes_the_holder_proof_bytes() -> None:
    case = CASES["valid_mcp_tool"]["input"]
    proof = caller_ticket.strict_loads(caller_ticket.b64url_decode(case["proof"]))
    body = {k: v for k, v in proof.items() if k != "sig"}
    again = caller_ticket.sign_proof(
        bytes.fromhex(DOC["test_keys"]["holder_seed_hex"]), body
    )
    assert again["sig"] == proof["sig"]
    ticket = caller_ticket.strict_loads(
        caller_ticket.b64url_decode(case["authorization"].split(" ")[1])
    )
    public = crypto.ed25519_public(bytes.fromhex(DOC["test_keys"]["holder_seed_hex"]))
    assert ticket["holder"] == caller_ticket.b64url_encode(public)


def test_generator_reproduces_the_committed_file() -> None:
    import generate_caller_vectors

    assert generate_caller_vectors.render() == VECTORS.read_text(encoding="utf-8")


def test_ordinary_authority_passes_beside_a_caller_revoke_that_is_present() -> None:
    case = CASES["caller_revoke_fold"]["input"]
    assert case["grants"] == []
    assert any(k.startswith("caller:") and k.endswith("/*/*") for k in case["revokes"])
    assert CASES["caller_revoke_fold"]["expect"]["ok"]["revoked"] == [case["caller"]]


def test_a_covering_agent_revoke_is_refused_by_the_same_check() -> None:
    control = CASES["caller_revoke_fold_control_refuses_covered_agent"]["input"]
    assert "agent-a/*/*" in control["revokes"]


def test_restart_pause_and_window_constants() -> None:
    assert caller_ticket.PROOF_WINDOW_MS == 120000
    assert caller_ticket.PROOF_AHEAD_MS == 5000
    assert caller_ticket.RESTART_PAUSE_MS == 5000
    assert caller_ticket.MINTER_KINDS == ("desktop", "pocket", "satellite")


def test_ticket_text_form_round_trips_to_the_authorization_header() -> None:
    text = CASES["ticket_form_accepted"]["input"]["text"]
    out = CASES["ticket_form_accepted"]["expect"]["ok"]
    assert out["authorization"] == "AstralCaller " + text.removeprefix(
        caller_ticket.TICKET_TEXT
    )


def test_a_restart_keeps_the_state_patches_made_before_it() -> None:
    case = CASES["restart_keeps_earlier_state_patches"]["input"]
    assert "restart" in case["steps"][1] and "state" not in case["steps"][1]


def test_lifetime_choices_are_the_contracted_set() -> None:
    assert caller_ticket.LIFETIMES == {
        "1h": 3600,
        "1d": 86400,
        "7d": 604800,
        "30d": 2592000,
    }
    assert caller_ticket.DEFAULT_LIFETIME == "7d"


def test_every_test_device_has_a_real_roster_kind() -> None:
    from astral_mesh_ref import roster

    state = CASES["valid_mcp_tool"]["input"]["state"]
    assert {m["kind"] for m in state["members"].values()} == set(roster.KINDS)


def test_undecodable_input_never_raises() -> None:
    state = CASES["valid_mcp_tool"]["input"]["state"]
    for header in ("AstralCaller \udcff", "AstralCaller " + "\u00e9" * 10, None, 7):
        out = caller_ticket.run(
            "caller_verify",
            {"state": state, "authorization": header, "proof": "x", "request": {}},
        )
        assert out == {"error": "caller.malformed"}
