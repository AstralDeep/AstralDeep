"""T022 (056-delegated-agent-chaining): explicit durable-consent capture.

Approving a schedule is the ONE moment a durable offline grant may be created
(FR-011): the consent card names the scopes being granted, its durable
365-day-capped nature, and how to revoke it; approval captures the session's
refresh token into an encrypted grant and links it onto the job. Nothing is
captured implicitly — no capture on proposal, on decline, or for a job that
runs no agent.
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import scheduling_chat  # noqa: E402


@pytest.fixture
def orch():
    o = MagicMock()
    o.history.db = MagicMock()
    # The card and the capture both read the EFFECTIVE scope list, so the
    # safe-baseline population (no explicit agent_scopes rows) is not told
    # "no scopes yet" and does not capture an empty consented list.
    o.tool_permissions.get_agent_scopes = MagicMock(
        return_value={"tools:read": True, "tools:search": True, "tools:write": False})
    o.tool_permissions.get_enabled_scope_names = MagicMock(
        return_value=["tools:read", "tools:search"])
    o.send_ui_render = AsyncMock()
    return o


@pytest.fixture
def captured(monkeypatch):
    """Stub the offline-grant + session stores; record what capture receives."""
    seen = {}

    grants = MagicMock()
    grants.capture = MagicMock(side_effect=lambda u, t, a: seen.update(
        user=u, token=t, agent=a) or "grant-new-1")
    monkeypatch.setattr("orchestrator.offline_grant.OfflineGrantStore",
                        MagicMock(return_value=grants))

    sessions = MagicMock()
    sessions.latest_refresh_token_for = MagicMock(return_value="refresh-abc")
    monkeypatch.setattr("orchestrator.session_store.WebSessionStore",
                        MagicMock(return_value=sessions))

    store = MagicMock()
    store.create_job = MagicMock(side_effect=lambda *a, **k: seen.update(
        job_kwargs=k) or {"id": "job-1"})
    monkeypatch.setattr("scheduler.store.ScheduledJobStore",
                        MagicMock(return_value=store))

    monkeypatch.setattr(scheduling_chat, "_audit", AsyncMock())
    seen["grants"] = grants
    seen["sessions"] = sessions
    return seen


def _proposal(orch, agent_id="web-research-1"):
    pid = "prop-1"
    orch._schedule_proposals = {pid: {
        "user_id": "u1", "chat_id": "c1", "created_at": time.time(),
        "args": {"name": "arXiv sweep", "instruction": "check arXiv",
                 "schedule_kind": "cron", "schedule_expr": "0 8 * * *",
                 "timezone": "UTC", "agent_id": agent_id},
    }}
    return pid


@pytest.fixture(autouse=True)
def _validate(monkeypatch):
    monkeypatch.setattr(
        scheduling_chat, "_validate_proposal",
        lambda orch, uid, args: (dict(args), 1_700_000_000_000))


@pytest.mark.asyncio
async def test_approval_captures_consent_and_links_grant(orch, captured):
    pid = _proposal(orch)
    await scheduling_chat.handle_decision(
        orch, MagicMock(), "u1",
        {"proposal_id": pid, "decision": "approve"})

    # The session's refresh token was captured into an encrypted grant...
    assert captured["user"] == "u1"
    assert captured["token"] == "refresh-abc"
    assert captured["agent"] == "web-research-1"
    # ...and linked onto the job (previously hardcoded None).
    assert captured["job_kwargs"]["offline_grant_id"] == "grant-new-1"
    # The consented scopes are the user's CURRENT enabled scopes, never wider.
    assert captured["job_kwargs"]["consented_scopes"] == ["tools:read", "tools:search"]


@pytest.mark.asyncio
async def test_decline_captures_nothing(orch, captured):
    pid = _proposal(orch)
    await scheduling_chat.handle_decision(
        orch, MagicMock(), "u1",
        {"proposal_id": pid, "decision": "discard"})
    captured["grants"].capture.assert_not_called()


@pytest.mark.asyncio
async def test_agentless_job_captures_nothing(orch, captured):
    """A job that runs no agent needs no durable agent authority."""
    pid = _proposal(orch, agent_id="")
    await scheduling_chat.handle_decision(
        orch, MagicMock(), "u1",
        {"proposal_id": pid, "decision": "approve"})
    captured["grants"].capture.assert_not_called()
    assert captured["job_kwargs"]["offline_grant_id"] is None


@pytest.mark.asyncio
async def test_no_live_session_creates_job_without_authority(orch, captured):
    """Fail-closed on the AUTHORITY, fail-open on the job: with no refresh
    token, the job exists but has no unattended grant (its first run skips)."""
    captured["sessions"].latest_refresh_token_for = MagicMock(return_value=None)
    pid = _proposal(orch)
    await scheduling_chat.handle_decision(
        orch, MagicMock(), "u1",
        {"proposal_id": pid, "decision": "approve"})
    captured["grants"].capture.assert_not_called()
    assert captured["job_kwargs"]["offline_grant_id"] is None


@pytest.mark.asyncio
async def test_capture_failure_is_not_fatal(orch, captured):
    captured["grants"].capture = MagicMock(
        side_effect=RuntimeError("OFFLINE_GRANT_ENC_KEY not configured"))
    pid = _proposal(orch)
    await scheduling_chat.handle_decision(
        orch, MagicMock(), "u1",
        {"proposal_id": pid, "decision": "approve"})
    assert captured["job_kwargs"]["offline_grant_id"] is None  # no fake authority


@pytest.mark.asyncio
async def test_consent_card_names_scopes_durability_and_revocation(orch):
    """FR-011: the card the user approves must SAY what it grants."""
    orch.tool_permissions.get_enabled_scope_names = MagicMock(
        return_value=["tools:read", "tools:search"])
    resp = await scheduling_chat.handle_meta_tool(
        orch, "schedule_recurring_task",
        {"name": "arXiv sweep", "instruction": "check arXiv",
         "schedule_kind": "cron", "schedule_expr": "0 8 * * *",
         "timezone": "UTC", "agent_id": "web-research-1"},
        user_id="u1", chat_id="c1", websocket=MagicMock())
    text = str(resp.ui_components)
    assert "durable consent" in text.lower()
    assert "tools:read" in text and "tools:search" in text  # the scopes granted
    assert "365 days" in text                               # the durability
    assert "revoke" in text.lower()                         # the revocation path
