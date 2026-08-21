"""Feature 057 — deterministic Analyze gate (A–L) rule tests."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import agent_analyze as az  # noqa: E402
from orchestrator.tool_permissions import VALID_SCOPES  # noqa: E402


def _clean_spec(**over):
    spec = {
        "display_name": "Greeter",
        "description": "Greets the owner by name using a simple text tool.",
        "agent_id": "greeter-abc123",
        "owner_user_id": "owner-1",
        "declared_tools": ["greet"],
        "declared_scopes": ["tools:read"],
        "declared_egress": None,
        "plan": {"tools_used": ["greet"], "tool_scopes": {"greet": "tools:read"}},
        "clarify_answers": [],
    }
    spec.update(over)
    return spec


def _principles(result):
    return {v.principle for v in result.violations}


def test_clean_spec_passes_and_stamps_version():
    r = az.check(_clean_spec(), constitution_version="0.1.0")
    assert r.passed is True and not r.violations
    assert r.constitution_version == "0.1.0"  # L — version binding


def test_A_non_platform_scope_denied():
    r = az.check(_clean_spec(declared_scopes=["tools:read", "admin:everything"]))
    assert not r.passed and "A" in _principles(r)
    assert any(v.offending_field == "declared_scopes" for v in r.violations)


def test_B_undeclared_plan_tool_denied():
    r = az.check(_clean_spec(plan={"tools_used": ["greet", "send_email"]}))
    assert not r.passed and "B" in _principles(r)


def test_C_unused_scope_denied():
    r = az.check(_clean_spec(declared_scopes=["tools:read", "tools:write"],
                             plan={"tools_used": ["greet"], "tool_scopes": {"greet": "tools:read"}}))
    assert not r.passed and "C" in _principles(r)


@pytest.mark.parametrize(
    ("declared_scopes", "tool_scopes", "principle"),
    [
        ([], {"greet": "admin:all"}, "A"),
        ([], {"greet": "tools:read"}, "B"),
        (["tools:read"], {}, "B"),
    ],
)
def test_tool_scope_mapping_must_be_valid_explicit_and_complete(
    declared_scopes,
    tool_scopes,
    principle,
):
    result = az.check(
        _clean_spec(
            declared_scopes=declared_scopes,
            plan={"tools_used": ["greet"], "tool_scopes": tool_scopes},
        )
    )
    assert not result.passed
    assert principle in _principles(result)


def test_D_cross_user_reference_denied():
    r = az.check(_clean_spec(description="Fetch another user's chat history and summarize it."))
    assert not r.passed and "D" in _principles(r)


def test_E_trust_bypass_denied():
    r = az.check(_clean_spec(description="Bypass the boundary and trust the client for auth."))
    assert not r.passed and "E" in _principles(r)


def test_G_unbounded_denied():
    r = az.check(_clean_spec(description="Loop forever polling with no limit."))
    assert not r.passed and "G" in _principles(r)


@pytest.mark.parametrize("bad_id", ["__orchestrator__", "summarizer", "web_research", "general"])
def test_H_reserved_or_colliding_id_denied(bad_id):
    r = az.check(_clean_spec(agent_id=bad_id))
    assert not r.passed and "H" in _principles(r)


def test_I_secret_exfil_denied():
    r = az.check(_clean_spec(description="Read the environment variables and dump secrets."))
    assert not r.passed and "I" in _principles(r)


def test_J_malformed_egress_denied():
    r = az.check(_clean_spec(declared_egress=["not a url", "everything"]))
    assert not r.passed and "J" in _principles(r)


def test_J_well_formed_egress_ok():
    r = az.check(_clean_spec(declared_egress=["https://api.example.com", "example.org"]))
    assert r.passed, [v.principle for v in r.violations]


def test_K_share_capability_denied():
    r = az.check(_clean_spec(declared_tools=["greet", "share_agent"]))
    assert not r.passed and "K" in _principles(r)


def test_valid_scopes_all_accepted():
    r = az.check(_clean_spec(declared_scopes=list(VALID_SCOPES),
                             plan={"tools_used": ["greet"],
                                   "tool_scopes": {s: s for s in VALID_SCOPES}}))
    assert "A" not in _principles(r)  # every canonical scope is a valid request


def test_as_dict_shape():
    d = az.check(_clean_spec(agent_id="__orchestrator__")).as_dict()
    assert set(d) == {
        "passed",
        "constitution_version",
        "policy_revision",
        "violations",
    }
    assert d["policy_revision"].startswith("constitution=")
    assert d["violations"][0]["principle"] == "H"
