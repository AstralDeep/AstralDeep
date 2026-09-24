"""Tests for orchestrator/tool_feedback.py: tool failures stay brief and never surface
provider text or diagnostics to chat, notices are shared across parallel calls within
a turn but not across turns, and pending cards drop replay arguments.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import tool_feedback
from shared.protocol import MCPResponse


@pytest.mark.parametrize("code", tool_feedback.PUBLIC_ERRORS)
def test_known_error_never_displays_provider_text(code):
    notice = tool_feedback.tool_failure_notice("web_search", {
        "code": code, "message": "<script>private API key and query</script>",
    })
    assert notice["type"] == "alert"
    assert notice["variant"] == "error"
    assert len(notice["message"]) < 160
    assert "private" not in str(notice)
    assert "title" not in notice
    assert notice["css"]["padding"] == "6px 10px"


@pytest.mark.parametrize("error", [None, "secret", {}, {"code": []}, {"code": "<unsafe>"}])
@pytest.mark.parametrize("tool", [None, "x" * 65, "[secret](https://private)", "\n<iframe>"])
def test_malformed_untrusted_errors_and_identifiers_are_not_reflected(tool, error):
    assert tool_feedback.tool_failure_notice(tool, error)["message"] == "Tool could not complete. Try again."


def test_safe_tool_label_and_turn_cleanup():
    assert tool_feedback.tool_failure_notice("search_arxiv", {})["message"].startswith("Search arxiv ")
    with pytest.raises(RuntimeError), tool_feedback.turn_tool_notices():
        assert tool_feedback.tool_failure_notice("x", {}) is not None
        assert tool_feedback.tool_failure_notice("x", {}) is None
        with tool_feedback.turn_tool_notices():
            assert tool_feedback.tool_failure_notice("x", {}) is not None
        assert tool_feedback.tool_failure_notice("x", {}) is None
        raise RuntimeError("test cleanup")
    assert tool_feedback.tool_failure_notice("x", {}) is not None


@pytest.mark.asyncio
async def test_parallel_calls_share_notices_but_concurrent_turns_do_not():
    async def notice(tool):
        await asyncio.sleep(0)
        return tool_feedback.tool_failure_notice(tool, {"code": "SEARCH_BLOCKED"})

    async def turn():
        with tool_feedback.turn_tool_notices():
            values = await asyncio.gather(notice("web_search"), notice("research_brief"))
            return sum(v is not None for v in values)

    assert await asyncio.gather(turn(), turn()) == [1, 1]


def test_memory_bound_does_not_suppress_unseen_errors():
    with tool_feedback.turn_tool_notices():
        for i in range(tool_feedback._MAX_NOTICES + 1):
            assert tool_feedback.tool_failure_notice(f"tool_{i}", {}) is not None
        assert len(tool_feedback._NOTICES.get()) == tool_feedback._MAX_NOTICES


@pytest.mark.parametrize("error", [
    "<script>private-key</script>", {"code": ["private-key"]},
    {"code": "SEARCH_BLOCKED", "message": "private-key", "retryable": False},
    {"code": "SEARCH_BLOCKED", "message": "private-key", "retryable": True},
    {"code": "private-key", "message": "private-key", "retryable": "private-key"},
])
def test_planner_errors_cannot_leak_diagnostics_or_claim_a_trusted_digest(error):
    from orchestrator.orchestrator import Orchestrator

    response = SimpleNamespace(error=error, result={"_model_digest": "private-key"})
    content = Orchestrator._tool_result_to_llm_content(response, "web_search")
    assert "private-key" not in content and "<script>" not in content
    assert not Orchestrator._result_has_model_digest(response)
    parsed = json.loads(content)
    assert parsed["status"] == "error"
    assert type(parsed["retryable"]) is bool
    assert parsed["retryable"] is (isinstance(error, dict) and error.get("retryable") is True)
    assert len(content) < 300


@pytest.mark.parametrize("data", [None, "unexpected", {}, {"status": "confirmation_required"}])
def test_pending_cards_do_not_persist_replay_arguments(data):
    from orchestrator.orchestrator import _tag_tool_result_source

    component = {"type": "card", "content": [{"type": "button", "action": "authorize_action"}]}
    response = SimpleNamespace(result={"_data": data} if data is not None else None)
    _tag_tool_result_source(component, response, "a1", "send_email", {"body": "PRIVATE-CONTENT"}, "audit1")
    assert component["_source_agent"] == "a1"
    assert component["_source_correlation_id"] == "audit1"
    if isinstance(data, dict) and data.get("status") == "confirmation_required":
        assert "PRIVATE-CONTENT" not in json.dumps(component)
        assert "_source_params" not in json.dumps(component)
    else:
        assert component["_source_params"] == {"body": "PRIVATE-CONTENT"}


@pytest.fixture
def orch(orchestrator_factory, monkeypatch):
    instance = orchestrator_factory()
    instance.audit_recorder = MagicMock()
    instance.audit_recorder.record = AsyncMock()
    instance.send_ui_render = AsyncMock()
    instance.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    instance.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
    instance._map_file_paths = lambda cid, args, **kwargs: args
    instance.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    instance.local_agents["a1"] = MagicMock()
    instance.lifecycle_manager._get_draft_by_agent_id = MagicMock(return_value=None)
    monkeypatch.setattr(instance, "_get_delegation_token", AsyncMock(return_value=None))
    return instance


def call(tool):
    return SimpleNamespace(function=SimpleNamespace(name=tool, arguments=json.dumps({})))


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_error_delivery_is_in_chat_and_preserves_diagnostics(orch, monkeypatch, parallel):
    response = MCPResponse(error={"code": "SEARCH_BLOCKED", "message": "private upstream HTML", "retryable": False})
    monkeypatch.setattr(orch, "_execute_with_retry", AsyncMock(return_value=response))
    with tool_feedback.turn_tool_notices():
        for _ in range(2):
            if parallel:
                results = await orch.execute_parallel_tools(
                    MagicMock(), [call("t1"), call("t2")], {"t1": "a1", "t2": "a1"}, "c1", user_id="u1")
            else:
                results = [await orch.execute_single_tool(
                    MagicMock(), call("t1"), {"t1": "a1"}, "c1", user_id="u1")]
            assert all(result.error["message"] == "private upstream HTML" for result in results)
    notices = [c for c in orch.send_ui_render.await_args_list if "Keyless search" in str(c)]
    assert len(notices) == 1
    assert notices[0].kwargs["target"] == "chat"
    assert len(notices[0].args[1]) == 1
    assert "private upstream" not in str(orch.send_ui_render.await_args_list)


@pytest.mark.asyncio
async def test_parallel_exception_never_reaches_chat_as_raw_text(orch, monkeypatch):
    monkeypatch.setattr(orch, "_execute_with_retry", AsyncMock(side_effect=RuntimeError("private traceback")))
    results = await orch.execute_parallel_tools(
        MagicMock(), [call("t1")], {"t1": "a1"}, "c1", user_id="u1")
    assert results[0].error
    calls = orch.send_ui_render.await_args_list
    assert calls[-1].kwargs["target"] == "chat"
    assert "private traceback" not in str(calls)
