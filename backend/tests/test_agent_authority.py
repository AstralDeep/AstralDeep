"""Pins orchestrator/agent_authority.py: node-signed in-process agent tokens match a golden vector made before the module split, children attenuate and stay bounded, and no token exists without bound machine authority.
It drives orchestrator.py's _mint_mcp_delegation_token and _get_delegation_token, shared/node_identity.py, async_tasks.VirtualWebSocket and the summarizer's token check."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator import agent_authority as aa
from shared import node_identity as ni

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "agent_authority_golden.json").read_text())
NOW = GOLDEN["now"]
OWNER = GOLDEN["owner"]
PAYLOAD = {
    "sub": OWNER, "act": {"sub": "agent:summarizer-1"}, "scope": "tools:read tool:read_value",
    "iss": "astral-internal-delegation", "aud": aa.AGENT_AUDIENCE, "iat": NOW, "exp": NOW + 300, "delegation": True,
}
PARENT = {
    "sub": OWNER, "scope": "tools:read tool:read tools:write tool:write", "iss": "astral-internal-delegation",
    "aud": aa.AGENT_AUDIENCE, "iat": NOW, "exp": NOW + 300, "delegation": True,
    "delegation_depth": 0, "max_delegation_depth": 3,
}


@pytest.fixture
def node(tmp_path, monkeypatch):
    directory = tmp_path / "node"
    directory.mkdir(mode=0o700)
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    (directory / ni.KEY_FILE).write_bytes(pem)
    os.chmod(directory / ni.KEY_FILE, 0o600)
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(directory))
    monkeypatch.setenv("ASTRAL_ENV", "development")
    for name in ("AGENT_SERVICE_CLIENT_ID", "DELEGATION_CHILD_SIGNING_KEY", "MEMORY_HMAC_KEY", ni.OWNER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(time, "time", lambda: float(NOW))
    ni.reset_cache()
    yield ni.load_node_identity()
    ni.reset_cache()


def test_the_node_identity_is_the_golden_one(node):
    assert node.node_id == GOLDEN["node_id"]


def test_encoded_tokens_match_the_golden_bytes(node):
    assert aa.encode_delegation_payload(PAYLOAD) == GOLDEN["encoded_payload"]
    assert aa.encode_delegation_payload({**PAYLOAD, "exp": NOW + 60}) == GOLDEN["encoded_short_lifetime"]
    assert aa.encode_delegation_payload({**PAYLOAD, "exp": NOW + 9999}) == GOLDEN["encoded_capped"]


def test_a_child_matches_the_golden_claims_and_bytes(node):
    parent = aa.normalize_hop_parent(dict(PARENT), "summarizer-1")
    assert parent == GOLDEN["parent"]
    child = aa.mint_child_delegation(parent, "worker-1", ["tools:read", "tool:read", "tools:admin"], now=NOW)
    assert child == GOLDEN["child"]
    token = aa.encode_delegation_payload(child)
    assert token == GOLDEN["child_token"]
    assert aa.decode_token_payload(token) == GOLDEN["decoded"]
    assert aa.verify_signed_delegation(token, audience=aa.AGENT_AUDIENCE) == GOLDEN["verified_claims"]


def test_chain_checks_and_the_audit_record_match_the_golden_values(node):
    child = GOLDEN["child"]
    assert aa.actor_chain(child) == GOLDEN["actor_chain"]
    assert list(aa.verify_delegation_chain(child, now=NOW)) == GOLDEN["verify_chain"]
    assert aa.delegation_chain_audit_record(GOLDEN["parent"], child, operation="read_value", tool="read_value", now=NOW) == GOLDEN["audit_record"]
    assert list(aa.authorize_chained_tool_call(child, "read", "tools:read", now=NOW)) == GOLDEN["authorize_in_scope"]
    assert list(aa.authorize_chained_tool_call(child, "write", "tools:read", now=NOW)) == GOLDEN["authorize_out_of_scope"]
    assert aa.attenuate_scopes(["b", "a", "c"], ["c", "a", "z"]) == GOLDEN["attenuated"]


def test_children_never_gain_scope_and_the_depth_limit_holds(node):
    parent = GOLDEN["parent"]
    child = aa.mint_child_delegation(parent, "worker-1", ["tools:read", "tools:admin", "tool:write"], now=NOW)
    assert set(child["scope"].split()) <= set(parent["scope"].split())
    assert "tools:admin" not in child["scope"].split()
    deepest = child
    for depth in range(2, aa.DEFAULT_MAX_DELEGATION_DEPTH + 1):
        deepest = aa.mint_child_delegation(deepest, f"worker-{depth}", ["tools:read"], now=NOW)
    assert deepest["delegation_depth"] == aa.DEFAULT_MAX_DELEGATION_DEPTH
    with pytest.raises(aa.DelegationDepthExceeded):
        aa.mint_child_delegation(deepest, "one-too-many", ["tools:read"], now=NOW)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda t: {**t, "act": {"sub": "agent:x", "act": {}}}, "actor chain is broken or incomplete"),
        (lambda t: {**t, "delegation_depth": 2}, "actor-chain length 2 inconsistent with depth 2"),
        (lambda t: {**t, "sub": "agent:someone"}, "chain does not terminate at a human principal"),
        (lambda t: {**t, "exp": NOW - 1000}, "delegation token expired"),
        (lambda t: {**t, "delegation_depth": 9}, "delegation depth 9 exceeds maximum 3"),
    ],
)
def test_a_malformed_chain_is_refused_with_its_reason(node, mutate, reason):
    ok, why = aa.verify_delegation_chain(mutate(GOLDEN["child"]), now=NOW)
    assert (ok, why) == (False, reason)


def test_signed_claims_must_match_the_presented_claims_and_a_foreign_key_is_refused(node, tmp_path, monkeypatch):
    token = GOLDEN["child_token"]
    ok, why = aa.verify_delegation_chain({**GOLDEN["child"], "scope": "tools:read tools:write"}, now=NOW, raw_token=token)
    assert (ok, why) == (False, "claims do not match the signed delegation")
    other = tmp_path / "other"
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(other))
    ni.reset_cache()
    with pytest.raises(aa.RecursiveDelegationError, match="delegation signature refused"):
        aa.verify_signed_delegation(token)
    with pytest.raises(aa.RecursiveDelegationError, match="algorithm refused"):
        aa.verify_signed_delegation("eyJhbGciOiJub25lIn0.e30.")


def test_tool_scope_checks_keep_their_no_fallthrough_rules():
    scopes = ["tool:get_system_status", "tool:modify_data"]
    assert aa.is_tool_in_scope("get_system_status", scopes) is True
    assert aa.is_tool_in_scope("other", scopes) is False
    assert aa.is_tool_in_scope("anything", ["openid", "profile"]) is True
    assert aa.is_tool_in_scope("anything", []) is True
    assert aa.is_tool_in_scope("fetch_page", ["tools:search"], "tools:read") is False
    assert aa.is_tool_in_scope("fetch_page", ["tools:read", "tool:other"], "tools:read") is False


