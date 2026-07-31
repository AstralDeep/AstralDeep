"""Recording-coverage integration test (FR-021 / SC-003).

For each authority boundary in research.md §R10, we verify that the
helper actually emits a row to the audit store via the recorder.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture
def wired_recorder(repo):
    """Provide a Recorder bound to the test repo and registered globally."""
    from audit.recorder import Recorder, set_recorder
    rec = Recorder(repo)
    set_recorder(rec)
    yield rec
    set_recorder(None)


def test_record_auth_event_writes_a_row(wired_recorder, repo):
    from audit.hooks import record_auth_event
    user = f"u-{uuid.uuid4().hex[:8]}"
    asyncio.run(record_auth_event(
        claims={"sub": user, "preferred_username": user},
        action="login",
        description="test login",
    ))
    items, _ = repo.list_for_user(user, limit=10)
    assert any(i.event_class == "auth" and i.action_type == "auth.login" for i in items)


def test_record_ws_action_writes_a_row(wired_recorder, repo):
    from audit.hooks import record_ws_action
    user = f"u-{uuid.uuid4().hex[:8]}"
    asyncio.run(record_ws_action(
        claims={"sub": user},
        action="chat_message",
        chat_id="chat-1",
        payload={"message": "hello there"},
    ))
    items, _ = repo.list_for_user(user, limit=10)
    assert any(i.action_type == "ws.chat_message" for i in items)
    # Message body must NOT be persisted; only its length
    target = next(i for i in items if i.action_type == "ws.chat_message")
    assert "message_length" in target.inputs_meta
    assert target.inputs_meta["message_length"] == len("hello there")


def test_tool_dispatch_audit_emits_paired_rows(wired_recorder, repo):
    from audit.hooks import ToolDispatchAudit
    user = f"u-{uuid.uuid4().hex[:8]}"

    async def run():
        async with ToolDispatchAudit(
            claims={"sub": user},
            agent_id="agent-x",
            tool_name="weather",
            chat_id=None,
            args_meta={"city": "Berlin"},
        ) as ctx:
            ctx.set_outputs_meta({"forecast_count": 3})

    asyncio.run(run())
    items, _ = repo.list_for_user(user, limit=10)
    # Must have both a *.start (in_progress) and a *.end (success)
    starts = [i for i in items if i.action_type == "tool.weather.start"]
    ends = [i for i in items if i.action_type == "tool.weather.end"]
    assert starts and ends
    assert starts[0].outcome == "in_progress"
    assert ends[0].outcome == "success"
    # Paired: same correlation_id
    assert starts[0].correlation_id == ends[0].correlation_id


def test_tool_dispatch_records_failure_outcome(wired_recorder, repo):
    from audit.hooks import ToolDispatchAudit
    user = f"u-{uuid.uuid4().hex[:8]}"

    async def run():
        async with ToolDispatchAudit(
            claims={"sub": user},
            agent_id="agent-x",
            tool_name="risky",
            chat_id=None,
        ) as ctx:
            ctx.set_outcome("failure", "boom")

    asyncio.run(run())
    items, _ = repo.list_for_user(user, limit=10)
    end = next(i for i in items if i.action_type == "tool.risky.end")
    assert end.outcome == "failure"
    assert end.outcome_detail == "boom"


def test_mcp_dispatch_matches_chat_audit_pair_and_preserves_hash_chain(
    wired_recorder,
    repo,
):
    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import MCPResponse

    user = f"u-{uuid.uuid4().hex[:8]}"
    orchestrator = Orchestrator.__new__(Orchestrator)
    chat_invocation = object()
    mcp_invocation = object()
    orchestrator.ui_sessions = {
        chat_invocation: {"sub": user},
        mcp_invocation: {"sub": user, "_invocation_channel": "mcp"},
    }

    async def emit_hook(*_args, **_kwargs):
        return None

    orchestrator.hooks = SimpleNamespace(emit=emit_hook)

    async def dispatch(*_args, **_kwargs):
        return MCPResponse(result={"ok": True})

    orchestrator._execute_with_retry = dispatch

    async def run():
        chat = await Orchestrator._execute_with_retry_audited(
            orchestrator,
            chat_invocation,
            "agent-x",
            "weather",
            {"city": "Berlin"},
            user_id=user,
        )
        mcp = await Orchestrator._execute_with_retry_audited(
            orchestrator,
            mcp_invocation,
            "agent-x",
            "weather",
            {"city": "Berlin"},
            user_id=user,
        )
        return chat, mcp

    chat_result, mcp_result = asyncio.run(run())
    items, _ = repo.list_for_user(user, limit=20)
    pairs = {
        result.correlation_id: [
            item for item in items if item.correlation_id == result.correlation_id
        ]
        for result in (chat_result, mcp_result)
    }
    expected_actions = {"tool.weather.start", "tool.weather.end"}
    assert {item.action_type for item in pairs[chat_result.correlation_id]} == expected_actions
    assert {item.action_type for item in pairs[mcp_result.correlation_id]} == expected_actions
    assert all(
        "invocation_channel" not in item.inputs_meta
        for item in pairs[chat_result.correlation_id]
    )
    assert all(
        item.inputs_meta["invocation_channel"] == "mcp"
        for item in pairs[mcp_result.correlation_id]
    )
    assert repo.verify_chain(user) is None


def test_legacy_user_actions_are_not_recorded(wired_recorder, repo):
    """Unauthenticated 'legacy' sentinel must not enter the audit log."""
    from audit.hooks import record_auth_event
    asyncio.run(record_auth_event(
        claims={"sub": "legacy"},
        action="login",
        description="legacy",
    ))
    items, _ = repo.list_for_user("legacy", limit=10)
    assert all(i.description != "legacy" for i in items)
