"""Tests that tool arguments are repaired or regenerated before recording and refused identically on both dispatch paths."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from orchestrator.orchestrator import Orchestrator
from orchestrator.tool_arguments import ArgumentError, model_error_text

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
          "required": ["city"]}


def call(tool, arguments):
    return SimpleNamespace(id="c1", function=SimpleNamespace(name=tool, arguments=arguments))


def bare(regenerated=None):
    orch = Orchestrator.__new__(Orchestrator)
    skill = SimpleNamespace(id="get_weather", name="get_weather", input_schema=SCHEMA)
    orch.agent_cards = {"weather-1": SimpleNamespace(skills=[skill])}
    orch._call_llm_json = AsyncMock(return_value=regenerated)
    return orch


async def prepare(orch, tc, agent="weather-1"):
    await orch._prepare_tool_calls(None, [tc], {tc.function.name: agent}, None)
    return tc


async def test_repaired_arguments_are_written_back():
    orch = bare()
    tc = await prepare(orch, call("get_weather", '{"city": "Rome",}'))
    assert tc.function.arguments == '{"city": "Rome"}'
    assert getattr(tc, "_astral_argument_error", None) is None
    orch._call_llm_json.assert_not_awaited()


async def test_schema_coercion_is_written_back():
    tc = await prepare(bare(), call("get_weather", '{"city": "Rome", "days": "3"}'))
    assert json.loads(tc.function.arguments) == {"city": "Rome", "days": 3}


async def test_valid_arguments_are_left_byte_for_byte():
    tc = await prepare(bare(), call("get_weather", '{"city":  "Rome"}'))
    assert tc.function.arguments == '{"city":  "Rome"}'


async def test_regeneration_is_used_once_when_repair_fails():
    orch = bare(regenerated={"city": "Rome"})
    tc = await prepare(orch, call("get_weather", "city=Rome"))
    assert json.loads(tc.function.arguments) == {"city": "Rome"}
    orch._call_llm_json.assert_awaited_once()
    assert orch._call_llm_json.await_args.kwargs["schema"] == SCHEMA
    assert orch._call_llm_json.await_args.kwargs["feature"] == "tool_argument_repair"


async def test_failed_regeneration_marks_the_call_and_keeps_the_raw_text():
    tc = await prepare(bare(regenerated=None), call("get_weather", "city=Rome"))
    assert tc.function.arguments == "city=Rome"
    assert tc._astral_argument_error.code == "invalid_json"


async def test_regenerated_arguments_that_fail_the_schema_are_rejected():
    tc = await prepare(bare(regenerated={"city": ["Rome"]}), call("get_weather", "{}"))
    assert tc._astral_argument_error.code == "schema_mismatch"


async def test_write_file_is_left_to_its_own_checks():
    orch = bare()
    tc = await prepare(orch, call("write_file", '{"path": "/x",'), agent="computer-use-1")
    assert tc.function.arguments == '{"path": "/x",' and not hasattr(tc, "_astral_argument_error")
    orch._call_llm_json.assert_not_awaited()


async def test_a_tool_without_a_schema_is_repaired_but_never_regenerated():
    orch = bare(regenerated={"city": "Rome"})
    fixed = await prepare(orch, call("mystery", '{"a": 1,}'), agent=None)
    assert fixed.function.arguments == '{"a": 1}'
    broken = await prepare(orch, call("mystery", "a=1"), agent=None)
    assert broken._astral_argument_error.code == "invalid_json"
    orch._call_llm_json.assert_not_awaited()


async def test_oversized_raw_arguments_are_not_sent_for_regeneration():
    orch = bare(regenerated={"city": "Rome"})
    tc = await prepare(orch, call("get_weather", "x" * 8001))
    assert tc._astral_argument_error.code == "invalid_json"
    orch._call_llm_json.assert_not_awaited()


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


async def test_a_marked_call_is_refused_the_same_way_on_both_paths(orch):
    error = ArgumentError("schema_mismatch", "city", "missing required field 'city'.", {"city": "<string>"})
    marked = call("t1", "{}")
    object.__setattr__(marked, "_astral_argument_error", error)
    single = await orch.execute_single_tool(MagicMock(), marked, {"t1": "a1"}, "c1", user_id="u1")
    parallel = (await orch.execute_parallel_tools(MagicMock(), [marked], {"t1": "a1"}, "c1", user_id="u1"))[0]
    for response in (single, parallel):
        assert response.error["code"] == "invalid_tool_arguments"
        assert response.error["message"] == model_error_text("t1", error)
        assert response.error["effect_state"] == "not_started"
        assert response.error["data"] == {"field": "city", "reason": "schema_mismatch"}


async def test_parallel_path_accepts_raw_control_characters(orch):
    results = await orch.execute_parallel_tools(
        MagicMock(), [call("t1", '{"q": "a\nb"}')], {"t1": "ghost"}, "c1", user_id="u1")
    assert "not valid JSON" not in ((results[0].error or {}).get("message", "") if results[0] else "")


async def test_a_failing_regeneration_call_leaves_the_call_marked():
    orch = bare()
    orch._call_llm_json = AsyncMock(side_effect=RuntimeError("provider down"))
    tc = await prepare(orch, call("get_weather", "city=Rome"))
    assert tc._astral_argument_error.code == "invalid_json"
    orch._call_llm_json.assert_awaited_once()


async def test_cancellation_during_regeneration_propagates():
    import asyncio
    orch = bare()
    orch._call_llm_json = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await prepare(orch, call("get_weather", "city=Rome"))


async def test_regeneration_is_skipped_inside_a_persistent_dispatch(monkeypatch):
    monkeypatch.setattr("persistent_agents.dispatch_context.current_dispatch", lambda: object())
    orch = bare(regenerated={"city": "Rome"})
    tc = await prepare(orch, call("get_weather", "city=Rome"))
    assert tc._astral_argument_error.code == "invalid_json"
    orch._call_llm_json.assert_not_awaited()


async def test_a_non_object_regeneration_is_rejected():
    tc = await prepare(bare(regenerated=["Rome"]), call("get_weather", "city=Rome"))
    assert tc._astral_argument_error.code == "invalid_json"
