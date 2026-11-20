"""Covers the non-personal signing path, issuer refusal, malformed-claim handling and mesh attenuation branches of delegation tokens.
It extends the personal-profile coverage in test_node_delegation.py for orchestrator/agent_authority.py."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from orchestrator import agent_authority as aa
from shared import node_identity as ni


@pytest.fixture
def shared_profile(monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    ni.reset_cache()
    yield
    ni.reset_cache()


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_non_personal_signing_is_refused_without_a_personal_node(shared_profile):
    with pytest.raises(aa.DelegationConfigError, match="personal node only"):
        aa.encode_delegation_payload({"sub": "u"})


def test_non_personal_encoding_signs_header_payload_with_hmac(shared_profile, monkeypatch):
    monkeypatch.setattr(aa, "_child_signing_key", lambda: b"k" * 32)
    payload = {"sub": "u", "scope": "a"}
    token = aa.encode_delegation_payload(payload)
    head, body, sig = token.split(".")
    assert json.loads(base64.urlsafe_b64decode(head + "==")) == {"alg": "HS256", "typ": "JWT"}
    assert aa.decode_token_payload(token) == payload
    expected = hmac.new(b"k" * 32, f"{head}.{body}".encode(), hashlib.sha256).digest()
    assert sig == b64(expected)


def test_signed_delegation_refuses_wrong_issuer(monkeypatch):
    monkeypatch.setattr(aa.node_identity, "peek_algorithm", lambda token: "EdDSA")
    monkeypatch.setattr(aa.node_identity, "is_personal", lambda: True)

    class Identity:
        def verify_token(self, token, audience=None):
            return {"iss": "other", "sub": "u"}

    monkeypatch.setattr(aa.node_identity, "load_node_identity", lambda create=True: Identity())
    with pytest.raises(aa.RecursiveDelegationError, match="issuer refused"):
        aa.verify_signed_delegation("t", issuer="astral-delegation")
    assert aa.verify_signed_delegation("t", issuer="other")["sub"] == "u"


def test_token_depth_tolerates_malformed_claim():
    assert aa._token_depth({aa.DELEGATION_DEPTH_CLAIM: "not-a-number"}) == 0
    assert aa._token_depth({aa.DELEGATION_DEPTH_CLAIM: [1]}) == 0
    assert aa._token_depth({aa.DELEGATION_DEPTH_CLAIM: "2"}) == 2


def test_actor_chain_walk_is_capped():
    node = {"sub": "agent:leaf"}
    for index in range(aa._ACTOR_CHAIN_WALK_CAP + 2):
        node = {"sub": f"agent:{index}", "act": node}
    actors, complete = aa._walk_actor_chain({"act": node})
    assert complete is False
    assert len(actors) == aa._ACTOR_CHAIN_WALK_CAP + 1


def test_actor_chain_with_malformed_link_is_incomplete():
    assert aa._walk_actor_chain({"act": {"sub": "a", "act": "bad"}}) == (["a"], False)
    assert aa._walk_actor_chain({"act": {"sub": "a", "act": {"no": "sub"}}}) == (["a"], False)


def test_normalize_hop_parent_passes_non_dict_through():
    assert aa.normalize_hop_parent("raw", "agent-1") == "raw"
    assert aa.normalize_hop_parent({"act": {"sub": "x"}}, "agent-1") == {"act": {"sub": "x"}}
    assert aa.normalize_hop_parent({}, "agent-1")["act"] == {"sub": "agent:agent-1"}


def parent_token(**extra):
    token = {"sub": "human", "act": {"sub": "agent:root"}, "scope": "tool:a tool:b",
             "exp": 2000, "aud": "astral-agent-service", "iss": "astral-delegation"}
    token.update(extra)
    return token


def test_child_inherits_mesh_identity_and_cnf():
    parent = parent_token(astral_mesh={"mesh": "m"}, cnf={"jkt": "k"})
    child = aa.mint_child_delegation(parent, "kid", ["tool:a"], now=1000)
    assert child["astral_mesh"] == {"mesh": "m"} and child["astral_mesh"] is not parent["astral_mesh"]
    assert child["cnf"] == {"jkt": "k"}
    assert child["scope"] == "tool:a"


@pytest.mark.parametrize("extra", [
    {"astral_mesh": {"mesh": "m"}},
    {"astral_mesh": {"mesh": "m"}, "cnf": "not-a-dict"},
    {"astral_mesh": "not-a-dict", "cnf": {"jkt": "k"}},
])
def test_child_refuses_incomplete_mesh_parent(extra):
    with pytest.raises(aa.RecursiveDelegationError, match="mesh parent identity is incomplete"):
        aa.mint_child_delegation(parent_token(**extra), "kid", ["tool:a"], now=1000)


def valid_token(**extra):
    token = {"sub": "human", "act": {"sub": "agent:a"}, "scope": "", "exp": 5000,
             aa.DELEGATION_DEPTH_CLAIM: 0}
    token.update(extra)
    return token


def test_verify_chain_accepts_valid_token_and_tolerates_bad_max_claim():
    assert aa.verify_delegation_chain(valid_token(), now=1000) == (True, "")
    ok, _ = aa.verify_delegation_chain(valid_token(**{aa.MAX_DEPTH_CLAIM: "junk"}), now=1000)
    assert ok is True


def test_verify_chain_refuses_negative_depth():
    ok, reason = aa.verify_delegation_chain(valid_token(**{aa.DELEGATION_DEPTH_CLAIM: -1}), now=1000)
    assert (ok, reason) == (False, "negative delegation depth")


def test_verify_chain_refuses_unexpected_human_principal():
    ok, reason = aa.verify_delegation_chain(valid_token(), now=1000, expected_human_sub="someone-else")
    assert (ok, reason) == (False, "human principal does not match expected authorizer")
    assert aa.verify_delegation_chain(valid_token(), now=1000, expected_human_sub="human")[0] is True


def test_verify_chain_refuses_expired_and_malformed_expiry():
    expired, reason = aa.verify_delegation_chain(valid_token(exp=100), now=1000)
    assert (expired, reason) == (False, "delegation token expired")
    assert aa.verify_delegation_chain(valid_token(exp=100), now=150)[0] is True
    bad, reason = aa.verify_delegation_chain(valid_token(exp="soon"), now=1000)
    assert (bad, reason) == (False, "malformed expiry")
