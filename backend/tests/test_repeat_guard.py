"""Tests for the per-turn repeat guard and its enforcement on both dispatch paths."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from orchestrator import repeat_guard
from orchestrator.orchestrator import Orchestrator
from orchestrator.repeat_guard import RepeatGuard
from shared.protocol import MCPResponse

ARGS = '{"path": "/tmp/x"}'


def call(name, arguments):
    return SimpleNamespace(id="c", function=SimpleNamespace(name=name, arguments=arguments))


def test_two_failures_warn_then_block_then_stop():
    guard = RepeatGuard()
    assert not guard.blocked("read_file", ARGS)
    verdicts = [guard.observe("read_file", ARGS, True) for _ in range(6)]
    assert verdicts == ["ok", "warn", "block", "block", "block", "stop"]
    assert guard.blocked("read_file", ARGS)


def test_blocked_only_after_the_second_failure():
    guard = RepeatGuard()
    guard.observe("read_file", ARGS, True)
    assert not guard.blocked("read_file", ARGS)
    guard.observe("read_file", ARGS, True)
    assert guard.blocked("read_file", ARGS)


def test_a_success_clears_the_count():
    guard = RepeatGuard()
    guard.observe("read_file", ARGS, True)
    guard.observe("read_file", ARGS, True)
    assert guard.observe("read_file", ARGS, False) == "ok"
    assert not guard.blocked("read_file", ARGS)


def test_identity_ignores_key_order_and_whitespace_but_not_values():
    guard = RepeatGuard()
    guard.observe("t", '{"a":1,"b":2}', True)
    assert guard.observe("t", '{ "b": 2, "a": 1 }', True) == "warn"
    assert guard.observe("t", '{"a":1,"b":3}', True) == "ok"
    assert guard.observe("other", '{"a":1,"b":2}', True) == "ok"


def test_unparseable_arguments_are_compared_as_text():
    guard = RepeatGuard()
    guard.observe("t", "city=Rome", True)
    assert guard.observe("t", " city=Rome ", True) == "warn"


def test_start_turn_installs_a_fresh_guard():
    first = repeat_guard.start_turn()
    assert repeat_guard.current() is first
    assert repeat_guard.start_turn() is not first


def test_is_failure():
    assert repeat_guard.is_failure(None)
    assert repeat_guard.is_failure(MCPResponse(error={"message": "boom"}))
    assert repeat_guard.is_failure(MCPResponse(result={"error": {"message": "boom"}}))
    assert repeat_guard.is_failure(MCPResponse(
        ui_components=[{"type": "alert", "variant": "error", "message": "boom"}]))
    assert not repeat_guard.is_failure(MCPResponse(result={"ok": True}))
    assert not repeat_guard.is_failure(MCPResponse(result={"status": "confirmation_required"}))


def test_apply_repeat_guard_warns_once_then_stops_the_turn():
    orch = Orchestrator.__new__(Orchestrator)
    repeat_guard.start_turn()
    failed = MCPResponse(error={"message": "boom"})
    messages = []
    stops = [orch._apply_repeat_guard([call("read_file", ARGS)], [failed], None, messages) for _ in range(6)]
    assert stops == [False, False, False, False, False, True]
    assert [m["content"] for m in messages] == [repeat_guard.warn_text("read_file")]


def test_apply_repeat_guard_without_a_guard_does_nothing():
    repeat_guard._CURRENT.set(None)
    orch = Orchestrator.__new__(Orchestrator)
    assert orch._apply_repeat_guard([call("t", "{}")], [None], None, []) is False


@pytest.fixture
def orch(orchestrator_factory, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")
    built = orchestrator_factory()
    built.audit_recorder = MagicMock()
    built.audit_recorder.record = AsyncMock()
    built.send_ui_render = AsyncMock()
    built.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    built.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
    built.tool_permissions.get_tool_scope_map = MagicMock(
        side_effect=lambda agent_id: {"t1": "tools:read"} if agent_id == "a1" else {})
    built._map_file_paths = lambda cid, a, **k: a
    built.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    built.local_agents["a1"] = MagicMock()
    return built


async def test_a_blocked_call_is_not_dispatched_on_either_path(orch):
    guard = repeat_guard.start_turn()
    guard.observe("t1", "{}", True)
    guard.observe("t1", "{}", True)
    tc = call("t1", "{}")
    single = await orch.execute_single_tool(MagicMock(), tc, {"t1": "a1"}, "c1", user_id="u1")
    parallel = (await orch.execute_parallel_tools(MagicMock(), [tc], {"t1": "a1"}, "c1", user_id="u1"))[0]
    for response in (single, parallel):
        assert response.error["code"] == "repeated_call_blocked"
        assert response.error["message"] == repeat_guard.blocked_text("t1")
    assert orch.local_agents["a1"].mock_calls == []

