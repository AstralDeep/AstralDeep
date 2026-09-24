"""Tests for backend/orchestrator/policy.py's deterministic pre-action rule engine:
predicate matching, allow/deny/confirm/rewrite effects, rule ordering, fail-open on
malformed rules, and loading rules from the environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import policy  # noqa: E402
from orchestrator.policy import PolicyDecision, evaluate_policy  # noqa: E402


def _ctx(tool="search", agent="a-1", roles=None, args=None):
    return {"tool": tool, "agent": agent, "user_id": "u",
            "roles": roles or [], "args": args or {}}


def test_policy_default_off(monkeypatch):
    monkeypatch.delenv("FF_POLICY_ENGINE", raising=False)
    assert policy.policy_enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_policy_on_values(monkeypatch, value):
    monkeypatch.setenv("FF_POLICY_ENGINE", value)
    assert policy.policy_enabled() is True


def test_no_rules_allows():
    d = evaluate_policy([], _ctx())
    assert d.effect == policy.ALLOW and d.args is None


def test_default_decision_is_allow():
    assert PolicyDecision().effect == policy.ALLOW


def test_tool_glob_deny():
    rules = [{"id": "no-delete", "when": {"tool": "delete_*"}, "effect": "deny",
              "reason": "deletion blocked"}]
    assert evaluate_policy(rules, _ctx(tool="delete_user")).effect == policy.DENY
    assert evaluate_policy(rules, _ctx(tool="search")).effect == policy.ALLOW


def test_agent_match():
    rules = [{"when": {"agent": "nefarious-*"}, "effect": "deny"}]
    assert evaluate_policy(rules, _ctx(agent="nefarious-1")).effect == policy.DENY
    assert evaluate_policy(rules, _ctx(agent="a-1")).effect == policy.ALLOW


def test_role_and_not_role():
    admin_only = [{"when": {"tool": "purge_*", "not_role": "admin"}, "effect": "deny"}]
    assert evaluate_policy(admin_only, _ctx(tool="purge_all", roles=["user"])).effect == policy.DENY
    assert evaluate_policy(admin_only, _ctx(tool="purge_all", roles=["admin"])).effect == policy.ALLOW

    needs_role = [{"when": {"role": "finance"}, "effect": "allow"}, {"effect": "deny"}]
    assert evaluate_policy(needs_role, _ctx(roles=["finance"])).effect == policy.ALLOW
    assert evaluate_policy(needs_role, _ctx(roles=["user"])).effect == policy.DENY


def test_args_regex():
    rules = [{"when": {"args_regex": "drop\\s+table"}, "effect": "deny"}]
    assert evaluate_policy(rules, _ctx(args={"q": "DROP TABLE users"})).effect == policy.DENY
    assert evaluate_policy(rules, _ctx(args={"q": "select 1"})).effect == policy.ALLOW


def test_predicate_is_anded():
    rules = [{"when": {"tool": "wire_*", "args_regex": "amount"}, "effect": "confirm"}]
    assert evaluate_policy(rules, _ctx(tool="wire_money", args={"amount": 100})).effect == policy.CONFIRM
    assert evaluate_policy(rules, _ctx(tool="wire_money", args={"to": "x"})).effect == policy.ALLOW


def test_first_terminal_rule_wins():
    rules = [
        {"id": "allow-search", "when": {"tool": "search"}, "effect": "allow"},
        {"id": "deny-all", "effect": "deny"},
    ]
    d = evaluate_policy(rules, _ctx(tool="search"))
    assert d.effect == policy.ALLOW and d.rule_id == "allow-search"
    assert evaluate_policy(rules, _ctx(tool="other")).effect == policy.DENY


def test_confirm_effect():
    rules = [{"when": {"tool": "send_email"}, "effect": "confirm", "reason": "confirm send"}]
    d = evaluate_policy(rules, _ctx(tool="send_email"))
    assert d.effect == policy.CONFIRM and d.reason == "confirm send"


def test_rewrite_redacts_then_allows():
    rules = [{"when": {"tool": "*"}, "effect": "rewrite", "rewrite": {"redact_args": ["password"]}}]
    d = evaluate_policy(rules, _ctx(args={"password": "hunter2", "user": "bob"}))
    assert d.effect == policy.ALLOW
    assert d.args == {"password": "[redacted by policy]", "user": "bob"}


def test_rewrite_accumulates_before_terminal():
    rules = [
        {"effect": "rewrite", "rewrite": {"redact_args": ["token"]}},
        {"when": {"tool": "deploy"}, "effect": "deny", "reason": "no deploy"},
    ]
    d = evaluate_policy(rules, _ctx(tool="deploy", args={"token": "abc"}))
    assert d.effect == policy.DENY
    assert d.args == {"token": "[redacted by policy]"}


def test_no_rewrite_leaves_args_none():
    rules = [{"when": {"tool": "search"}, "effect": "allow"}]
    assert evaluate_policy(rules, _ctx(args={"q": "x"})).args is None


@pytest.mark.parametrize("bad", [
    {"effect": "frobnicate"},
    {"when": "not-a-dict", "effect": "deny"},
    "not-a-dict",
    {"when": {"args_regex": "([unclosed"}, "effect": "deny"},
])
def test_malformed_rule_never_blocks(bad):
    assert evaluate_policy([bad], _ctx()).effect == policy.ALLOW


def test_good_rule_after_bad_still_applies():
    rules = [{"effect": "frob"}, {"when": {"tool": "x"}, "effect": "deny"}]
    assert evaluate_policy(rules, _ctx(tool="x")).effect == policy.DENY


def test_load_rules_from_env(monkeypatch):
    monkeypatch.setenv("POLICY_RULES", '[{"when":{"tool":"x"},"effect":"deny"}]')
    rules = policy.load_rules()
    assert rules and rules[0]["effect"] == "deny"


def test_load_rules_bad_json_falls_back(monkeypatch):
    monkeypatch.setenv("POLICY_RULES", "{not json")
    assert policy.load_rules() == policy._SEED_RULES


def test_load_rules_non_list_falls_back(monkeypatch):
    monkeypatch.setenv("POLICY_RULES", '{"a":1}')
    assert policy.load_rules() == policy._SEED_RULES


def test_load_rules_unset_is_seeds(monkeypatch):
    monkeypatch.delenv("POLICY_RULES", raising=False)
    assert policy.load_rules() == policy._SEED_RULES
