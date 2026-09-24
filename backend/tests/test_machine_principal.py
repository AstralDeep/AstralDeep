"""Tests for audit/hooks.py: machine-context claims resolve to a machine:<class>
actor-principal ahead of the legacy fallback so machine-initiated turns are recorded
rather than dropped; interactive turns are unaffected.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import audit.hooks as hooks  # noqa: E402
from audit.hooks import ToolDispatchAudit, actor_principal_from_claims  # noqa: E402


def test_machine_claims_resolve_to_machine_principal():
    for turn_class in ("scheduled_job", "parser_replay", "draft_self_test"):
        user, principal = actor_principal_from_claims(
            {"sub": "owner-1", "machine_class": turn_class,
             "consent_ref": "g1"})
        assert user == "owner-1"
        assert principal == f"machine:{turn_class}"


def test_machine_claims_without_owner_stay_legacy():
    user, principal = actor_principal_from_claims(
        {"machine_class": "scheduled_job"})
    assert user == "legacy"


def test_interactive_claims_unchanged():
    user, principal = actor_principal_from_claims({"sub": "u1"})
    assert (user, principal) == ("u1", "u1")
    user, principal = actor_principal_from_claims(
        {"sub": "u1", "act": {"sub": "agent:a1"}})
    assert (user, principal) == ("u1", "agent:a1")


def test_absent_claims_still_legacy():
    assert actor_principal_from_claims(None) == ("legacy", "legacy")
    assert actor_principal_from_claims({}) == ("legacy", "legacy")


def _capture_recorder(monkeypatch):
    rec = MagicMock()
    rec.record = AsyncMock()
    monkeypatch.setattr(hooks, "get_recorder", lambda: rec)
    return rec


@pytest.mark.asyncio
async def test_machine_turn_tool_dispatch_is_recorded(monkeypatch):
    rec = _capture_recorder(monkeypatch)
    machine_claims = {"sub": "owner-1", "machine_class": "scheduled_job",
                      "consent_ref": "grant-9"}
    async with ToolDispatchAudit(
            claims=machine_claims, agent_id="a1", tool_name="web_search",
            chat_id="c1", args_meta={"query": "arxiv sdui"}):
        pass
    assert rec.record.await_count == 2
    start, end = (call.args[0] for call in rec.record.await_args_list)
    for row in (start, end):
        assert row.actor_user_id == "owner-1"
        assert row.auth_principal == "machine:scheduled_job"
        assert row.inputs_meta.get("consent_ref") == "grant-9"
    assert start.action_type == "tool.web_search.start"
    assert end.action_type == "tool.web_search.end"
    assert start.correlation_id == end.correlation_id


@pytest.mark.asyncio
async def test_claimless_turn_still_dropped(monkeypatch):
    rec = _capture_recorder(monkeypatch)
    async with ToolDispatchAudit(claims=None, agent_id="a1",
                                 tool_name="t", chat_id=None):
        pass
    rec.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_turn_records_unchanged(monkeypatch):
    rec = _capture_recorder(monkeypatch)
    async with ToolDispatchAudit(claims={"sub": "u1"}, agent_id="a1",
                                 tool_name="t", chat_id="c1"):
        pass
    assert rec.record.await_count == 2
    start = rec.record.await_args_list[0].args[0]
    assert start.actor_user_id == "u1"
    assert start.auth_principal == "u1"
    assert "consent_ref" not in start.inputs_meta
