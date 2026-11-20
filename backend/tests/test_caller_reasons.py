"""Checks that every refusal code the caller door can raise carries an actionable reason, so an outside agent is never left with a bare code."""

from orchestrator import caller_tickets


def test_every_status_mapped_code_has_a_specific_reason():
    codes = caller_tickets.UNAUTHORIZED | caller_tickets.FORBIDDEN | caller_tickets.UNAVAILABLE
    codes |= {caller_tickets.BEARER_REFUSED, caller_tickets.MESH_UNAVAILABLE, caller_tickets.OWNER_UNBOUND}
    missing = [code for code in codes if code not in caller_tickets.REASONS]
    assert missing == []


def test_unknown_and_gate_codes_still_get_a_next_step():
    assert "owner" in caller_tickets.reason_for("caller.never_seen")
    assert "safety gate" in caller_tickets.reason_for("gate.phi")
