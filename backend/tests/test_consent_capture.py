"""T022 (056-delegated-agent-chaining): explicit durable-consent capture.

Approving a schedule is the ONE moment a durable offline grant may be created
(FR-011): the consent card names the scopes being granted, its durable
365-day-capped nature, and how to revoke it; approval captures the session's
refresh token into an encrypted grant and links it onto the job. Nothing is
captured implicitly — no capture on proposal or on decline.

Agent-less jobs (proposed from chat without a specific agent) capture too:
every machine turn needs a grant (``MachineTurnAuthority.derive`` is
agent-independent) and the run is an ordinary assistant turn whose tool calls
route across ALL of the user's enabled agents, so the consented scope list is
the union of their effective scopes (``tool_visibility.enabled_scope_union``).
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
def captured(monkeypatch, orch):
    """Stub the offline-grant + session stores; record what capture receives."""
    seen = {}

    grants = MagicMock()
    grants.capture = MagicMock(side_effect=lambda u, t, a: seen.update(
        user=u, token=t, agent=a) or "grant-new-1")
    orch.offline_grants = grants

    sessions = MagicMock()
    sessions.latest_refresh_token_for = MagicMock(return_value="refresh-abc")
    orch.web_sessions = sessions

    store = MagicMock()
    store.create_job = MagicMock(side_effect=lambda *a, **k: seen.update(
        job_kwargs=k) or {"id": "job-1"})
    orch.scheduled_job_store = store

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
async def test_agentless_job_captures_union_consent(orch, captured, monkeypatch):
    """Defect (reproduced live): an agent-less job used to be created with
    consented_scopes=[] and NO grant, so every run settled
    skipped_auth/missing_consent. Approval must capture a user-wide grant
    (agent_id=None) over the union of the user's enabled agents' scopes."""
    from orchestrator import tool_visibility
    monkeypatch.setattr(tool_visibility, "enabled_scope_union",
                        lambda o, uid: ["tools:read", "tools:search", "tools:files"])
    audit = AsyncMock()
    monkeypatch.setattr(scheduling_chat, "_audit", audit)
    ws = MagicMock()
    pid = _proposal(orch, agent_id="")
    await scheduling_chat.handle_decision(
        orch, ws, "u1", {"proposal_id": pid, "decision": "approve"})

    captured["grants"].capture.assert_called_once()
    assert captured["user"] == "u1"
    assert captured["token"] == "refresh-abc"
    assert captured["agent"] is None                     # user-wide grant
    assert captured["job_kwargs"]["offline_grant_id"] == "grant-new-1"
    assert captured["job_kwargs"]["agent_id"] == ""      # attribution untouched
    assert captured["job_kwargs"]["consented_scopes"] == [
        "tools:read", "tools:search", "tools:files"]
    # The per-agent helper is NOT consulted for an agent-less job.
    orch.tool_permissions.get_enabled_scope_names.assert_not_called()

    # Audit rows: consent_captured with agent_id null + the union; create
    # reports durable consent.
    by_type = {c.args[1]: c for c in audit.await_args_list}
    meta = by_type["schedule.consent_captured"].kwargs["inputs_meta"]
    assert meta["agent_id"] is None
    assert meta["consented_scopes"] == ["tools:read", "tools:search", "tools:files"]
    assert meta["grant_id"] == "grant-new-1"
    create = by_type["schedule.create"].kwargs["inputs_meta"]
    assert create["durable_consent"] is True
    assert create["consented_scopes"] == ["tools:read", "tools:search", "tools:files"]

    # The success Alert carries the same offline hint as agent-bound jobs.
    text = str(orch.send_ui_render.await_args.args[1])
    assert "run while you are signed out" in text
    assert "revoke" in text.lower()


@pytest.mark.asyncio
async def test_agentless_job_union_failure_fails_closed(orch, captured, monkeypatch):
    """A union derivation error must never widen: the job captures [] scopes
    (a grant still exists, but derive() asserts nothing for an empty list)."""
    from orchestrator import tool_visibility

    def boom(o, uid):
        raise RuntimeError("db down")
    monkeypatch.setattr(tool_visibility, "enabled_scope_union", boom)
    pid = _proposal(orch, agent_id="")
    await scheduling_chat.handle_decision(
        orch, MagicMock(), "u1", {"proposal_id": pid, "decision": "approve"})
    assert captured["job_kwargs"]["consented_scopes"] == []


@pytest.mark.asyncio
async def test_agentless_no_session_alert_says_cannot_run_signed_out(orch, captured):
    captured["sessions"].latest_refresh_token_for = MagicMock(return_value=None)
    pid = _proposal(orch, agent_id="")
    await scheduling_chat.handle_decision(
        orch, MagicMock(), "u1", {"proposal_id": pid, "decision": "approve"})
    captured["grants"].capture.assert_not_called()
    assert captured["job_kwargs"]["offline_grant_id"] is None
    text = str(orch.send_ui_render.await_args.args[1])
    assert "cannot run while you are signed out" in text


@pytest.mark.asyncio
async def test_agentless_consent_card_tells_the_truth(orch, monkeypatch):
    """The card must not claim 'runs without agent tools' — the run routes
    tools across every enabled agent; it names the union and the grant."""
    from orchestrator import tool_visibility
    monkeypatch.setattr(tool_visibility, "enabled_scope_union",
                        lambda o, uid: ["tools:read", "tools:search"])
    resp = await scheduling_chat.handle_meta_tool(
        orch, "schedule_recurring_task",
        {"name": "paper sweep", "instruction": "find new agentic-AI papers",
         "schedule_kind": "cron", "schedule_expr": "0 9 * * *",
         "timezone": "UTC", "agent_id": None},
        user_id="u1", chat_id="c1", websocket=MagicMock())
    text = str(resp.ui_components)
    assert "without agent tools" not in text
    assert "enabled agents" in text
    assert "durable consent" in text.lower()
    assert "tools:read" in text and "tools:search" in text
    assert "365 days" in text and "revoke" in text.lower()


def _visibility_orch(agents, *, disabled=(), drafts=(), scopes_by_agent=None):
    """Minimal orchestrator double for tool_visibility.enabled_scope_union."""
    o = MagicMock()
    cards = {}
    for aid in agents:
        card = MagicMock()
        card.skills = [MagicMock(id=f"{aid}_tool")]
        card.required_identity = None
        cards[aid] = card
    o.agent_cards = cards
    o.agents = {}
    o.local_agents = {aid: MagicMock() for aid in agents}
    o.security_flags = {}
    o._is_draft_agent = MagicMock(side_effect=lambda aid: aid in set(drafts))
    o.tool_permissions.list_disabled_agents = MagicMock(return_value=list(disabled))
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.tool_permissions.get_enabled_scope_names = MagicMock(
        side_effect=lambda uid, aid: (scopes_by_agent or {}).get(aid, []))
    return o


def test_union_excludes_drafts_disabled_and_not_connected(monkeypatch):
    from orchestrator import tool_visibility
    monkeypatch.setattr(tool_visibility, "identity_requirement_satisfied",
                        lambda card, claims: True)
    o = _visibility_orch(
        ["a-live", "a-draft", "a-disabled", "a-gone"],
        disabled=["a-disabled"], drafts=["a-draft"],
        scopes_by_agent={"a-live": ["tools:search", "tools:read"],
                         "a-draft": ["tools:execute"],
                         "a-disabled": ["tools:system"],
                         "a-gone": ["tools:files"]})
    del o.local_agents["a-gone"]  # registered card, not connected
    out = tool_visibility.enabled_scope_union(o, "u1")
    assert out == ["tools:read", "tools:search"]  # VALID_SCOPES order
    consulted = {c.args[1] for c in o.tool_permissions.get_enabled_scope_names.call_args_list}
    assert consulted == {"a-live"}


def test_union_merges_across_agents(monkeypatch):
    from orchestrator import tool_visibility
    monkeypatch.setattr(tool_visibility, "identity_requirement_satisfied",
                        lambda card, claims: True)
    o = _visibility_orch(["a1", "a2"], scopes_by_agent={
        "a1": ["tools:read"], "a2": ["tools:read", "tools:write"]})
    assert tool_visibility.enabled_scope_union(o, "u1") == ["tools:read", "tools:write"]


def test_union_fails_closed_on_error():
    from orchestrator import tool_visibility
    o = _visibility_orch(["a1"], scopes_by_agent={"a1": ["tools:read"]})
    o.tool_permissions.list_disabled_agents = MagicMock(side_effect=RuntimeError("db"))
    assert tool_visibility.enabled_scope_union(o, "u1") == []


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
