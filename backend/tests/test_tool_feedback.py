"""Tests for orchestrator/tool_feedback.py: tool failure notices stay brief and never
surface provider text or diagnostics to chat, and pending cards drop replay arguments.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator import tool_feedback


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


@pytest.mark.parametrize("code", ["REMOTE_MCP_REVIEW_UNAVAILABLE", "ACTION_REVIEW_UNAVAILABLE"])
def test_review_refusal_explains_no_execution_without_leaking_arguments(code):
    error = {"code": code, "message": "private patient and credential detail", "retryable": False}
    notice = tool_feedback.tool_failure_notice("call_remote_tool", error)
    assert notice["message"] == tool_feedback.PUBLIC_ERRORS[code]
    assert notice["message"].startswith("No ")
    assert "private patient and credential detail" not in json.dumps(notice)


