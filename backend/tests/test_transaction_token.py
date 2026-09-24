"""Tests for orchestrator/transaction_token.py: the HMAC mint/verify primitive (binding,
expiry, tamper, fail-closed without a key), args-hash intent semantics, the
single-use ConsumedStore, and policy.py's require_token effect.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import transaction_token as txn  # noqa: E402


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TXN_TOKEN_KEY", "unit-test-signing-key")
    monkeypatch.delenv("MEMORY_HMAC_KEY", raising=False)


def _store():
    return txn.ConsumedStore()


def test_mint_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("TXN_TOKEN_KEY", raising=False)
    monkeypatch.delenv("MEMORY_HMAC_KEY", raising=False)
    assert txn.mint("a", "u", "t", {"x": 1}) is None


def test_verify_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv("TXN_TOKEN_KEY", raising=False)
    monkeypatch.delenv("MEMORY_HMAC_KEY", raising=False)
    ok, reason = txn.verify("anything", "a", "u", "t", {})
    assert ok is False and reason == "signing disabled"


def test_memory_hmac_key_is_the_fallback(monkeypatch):
    monkeypatch.delenv("TXN_TOKEN_KEY", raising=False)
    monkeypatch.setenv("MEMORY_HMAC_KEY", "fallback-key")
    tok = txn.mint("a", "u", "t", {"x": 1})
    assert tok and txn.verify(tok, "a", "u", "t", {"x": 1})[0] is True


def test_round_trip_ok(key):
    tok = txn.mint("agent-1", "u-1", "transfer", {"amount": 5})
    ok, payload = txn.verify(tok, "agent-1", "u-1", "transfer", {"amount": 5})
    assert ok is True and payload["n"]


@pytest.mark.parametrize("agent,user,tool", [
    ("other", "u-1", "transfer"), ("agent-1", "other", "transfer"),
    ("agent-1", "u-1", "other_tool"),
])
def test_binding_mismatch_is_rejected(key, agent, user, tool):
    tok = txn.mint("agent-1", "u-1", "transfer", {"amount": 5})
    ok, reason = txn.verify(tok, agent, user, tool, {"amount": 5})
    assert ok is False and reason == "binding mismatch"


def test_args_retarget_is_rejected(key):
    tok = txn.mint("a", "u", "transfer", {"amount": 5})
    ok, reason = txn.verify(tok, "a", "u", "transfer", {"amount": 500})
    assert ok is False and reason == "args mismatch"


def test_args_hash_is_order_independent():
    assert txn.args_hash({"a": 1, "b": 2}) == txn.args_hash({"b": 2, "a": 1})


def test_args_hash_ignores_system_keys(key):
    tok = txn.mint("a", "u", "t", {"q": "hi"})
    ok, _ = txn.verify(tok, "a", "u", "t",
                       {"q": "hi", "_txn_token": tok, "_credentials": "x"})
    assert ok is True


def test_expiry(key):
    tok = txn.mint("a", "u", "t", {}, ttl_s=1, now_ms=0)
    assert txn.verify(tok, "a", "u", "t", {}, now_ms=500)[0] is True
    ok, reason = txn.verify(tok, "a", "u", "t", {}, now_ms=2000)
    assert ok is False and reason == "expired"


@pytest.mark.parametrize("mangle", [
    lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),
    lambda t: "AAAA" + t[4:],
    lambda t: t.replace(".", "", 1),
])
def test_tampered_token_is_invalid(key, mangle):
    tok = txn.mint("a", "u", "t", {"x": 1})
    ok, reason = txn.verify(mangle(tok), "a", "u", "t", {"x": 1})
    assert ok is False and reason == "invalid token"


@pytest.mark.parametrize("bad", [None, "", "no-dot", 123, "a.b.c.d"])
def test_malformed_token_is_invalid(key, bad):
    ok, reason = txn.verify(bad, "a", "u", "t", {})
    assert ok is False and reason == "invalid token"


def test_token_from_other_key_is_invalid(key, monkeypatch):
    tok = txn.mint("a", "u", "t", {"x": 1})
    monkeypatch.setenv("TXN_TOKEN_KEY", "a-different-key")
    assert txn.verify(tok, "a", "u", "t", {"x": 1})[0] is False


def test_single_use_consume(key):
    store = _store()
    tok = txn.mint("a", "u", "t", {"x": 1})
    assert txn.verify_and_consume(store, tok, "a", "u", "t", {"x": 1}) == (True, "ok")
    ok, reason = txn.verify_and_consume(store, tok, "a", "u", "t", {"x": 1})
    assert ok is False and reason == "already used"


def test_verify_and_consume_rejects_bad_before_consuming(key):
    store = _store()
    tok = txn.mint("a", "u", "t", {"x": 1})
    assert txn.verify_and_consume(store, tok, "a", "u", "t", {"x": 2})[0] is False
    assert txn.verify_and_consume(store, tok, "a", "u", "t", {"x": 1})[0] is True


def test_consumed_store_prunes_expired():
    store = _store()
    assert store.consume("n1", exp_ms=100, now_ms=0) is True
    assert store.consume("n1", exp_ms=100, now_ms=200) is True


def test_distinct_nonces_independent():
    store = _store()
    assert store.consume("n1", 1000, now_ms=0) is True
    assert store.consume("n2", 1000, now_ms=0) is True
    assert store.consume("n1", 1000, now_ms=0) is False


def test_default_store_is_process_singleton():
    assert txn.default_store() is txn.default_store()


def test_require_token_is_a_terminal_policy_effect():
    from orchestrator import policy
    rules = [{"when": {"tool": "transfer_*"}, "effect": "require_token",
              "reason": "transfers need a one-time token"}]
    d = policy.evaluate_policy(rules, {"tool": "transfer_funds", "agent": "a",
                                       "user_id": "u", "roles": [], "args": {}})
    assert d.effect == policy.REQUIRE_TOKEN and "one-time" in d.reason
    other = policy.evaluate_policy(rules, {"tool": "search", "agent": "a",
                                           "user_id": "u", "roles": [], "args": {}})
    assert other.effect == policy.ALLOW
