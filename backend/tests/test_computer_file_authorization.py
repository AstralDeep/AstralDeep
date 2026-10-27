"""Exercises file scope checks before review and the no-card behavior of bypass consent.
Uses the real computer dispatcher fixtures alongside pure path resolution cases.
"""

import json
from unittest.mock import AsyncMock

import pytest

from orchestrator import computer_use_policy as policy
from orchestrator import remote_confirmation as rc
from tests.test_computer_bypass_102 import _bypassed
from tests.test_computer_grant_flow_102 import CHAT, OWNER, ROOT, SCOPES, _call, _mac, _payload
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_proposals():
    rc._PENDING_CARDS.clear()
    yield
    rc._PENDING_CARDS.clear()


@pytest.mark.parametrize("path,expected", [
    ("hello.md", ROOT + "/hello.md"),
    (ROOT + "/sub/../hello.md", ROOT + "/hello.md"),
    (ROOT + "/hello.md", ROOT + "/hello.md"),
])
def test_file_paths_resolve_only_inside_the_shared_folder(path, expected):
    assert policy.resolve_workspace_path(path, [{"name": "x", "path": ROOT}], "Mac") == expected


@pytest.mark.parametrize("path", ["../hello.md", "/Users/bruh/hello.md", ROOT + "-other/a", "", "a\x00b"])
def test_invalid_file_paths_return_actual_shared_roots(path):
    with pytest.raises(policy.CwdRefusal) as caught:
        policy.resolve_workspace_path(path, [{"name": "x", "path": ROOT}], "Mac")
    assert ROOT in str(caught.value)


def test_no_shared_folder_provides_a_useful_failure():
    with pytest.raises(policy.CwdRefusal, match="shares no"):
        policy.resolve_workspace_path("hello.md", [], "Mac")


@pytest.mark.parametrize("tool", ["read_file", "list_dir", "write_file", "delete_path"])
async def test_outside_file_path_never_creates_an_approval(task_runtime, tool):
    orch, _ws, effects, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, tool: policy.SCOPES[tool]}
    response = await _call(task_runtime, tool, path="/Users/bruh/hello.md", content="poem")
    assert response.error and ROOT in response.error["message"]
    assert not effects and not rc._PENDING_CARDS
    assert not getattr(orch, "_hitl_pending_calls", {})


@pytest.mark.parametrize("tool,args", [
    ("run_command", {"argv": ["sudo", "ls"], "cwd": ROOT}),
    ("type_text", {"text": "secret", "hwnd": 50}),
    ("click", {"x": 1, "y": 1, "hwnd": 52}),
    ("type_text", {"text": "x"}),
])
async def test_bypass_denials_never_fall_through_to_an_approval(task_runtime, tool, args):
    orch, _ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    response = await _call(task_runtime, tool, **args)
    assert response.error and not effects
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})
    assert "confirmation_required" not in json.dumps(response.result)


@pytest.mark.parametrize("tool,args", [
    ("start_session", {}),
    ("open_app", {"app": "Terminal"}),
    ("confirm_action", {"summary": "Complete the requested task"}),
    ("resume_session", {}),
])
async def test_bypass_supports_session_and_terminal_actions_without_a_card(task_runtime, tool, args):
    orch, _ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    response = await _call(task_runtime, tool, **args)
    assert response.error is None and response.result["_data"]["approval_source"] == "bypass"
    assert [name for name, _args in effects] == [tool]
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


@pytest.mark.parametrize("tool", ["write_file", "delete_path"])
async def test_one_remote_file_review_releases_only_one_exact_effect(task_runtime, monkeypatch, tool):
    orch, ws, effects, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, tool: policy.SCOPES[tool]}
    monkeypatch.setattr(orch.computer_hosts, "request", AsyncMock(return_value={"text": "old"}))
    args = {"path": ROOT + "/hello.md", **({"content": "poem"} if tool == "write_file" else {})}
    response = await _call(task_runtime, tool, **args)
    payload = _payload(response, "approve")
    await rc.handle_decision(orch, ws, OWNER, payload)
    assert [name for name, _args in effects] == [tool]
    assert not getattr(orch, "_hitl_pending_calls", {})
    await rc.handle_decision(orch, ws, OWNER, payload)
    assert len(effects) == 1


async def test_server_failure_pause_can_be_captured_and_reviewed_again(task_runtime, monkeypatch):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=True)
    host.websocket = ws
    authority = rc.capture_computer_approval(orch, ws, OWNER, CHAT, {})
    await rc.pause_failed_computer_approval(orch, ws, authority)
    captured = rc.capture_computer_approval(orch, ws, OWNER, CHAT, {})
    assert captured is not None
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": policy.SCOPES["write_file"]}
    monkeypatch.setattr(orch.computer_hosts, "request", AsyncMock(return_value={"text": "old"}))
    response = await _call(task_runtime, "write_file", path=ROOT + "/hello.md", content="new")
    await rc.handle_decision(orch, ws, OWNER, _payload(response, "approve_and_resume"))
    assert session.state == "active" and len(effects) == 1


@pytest.mark.parametrize("change", ["task", "chat", "generation", "manual_pause", "unanchored"])
async def test_server_pause_never_recovers_changed_or_unanchored_authority(task_runtime, change):
    from orchestrator import hitl_confirmation as hc

    orch, ws, *_ = task_runtime
    session, _host = await _mac(task_runtime, remote=True)
    authority = rc.capture_computer_approval(orch, ws, OWNER, CHAT, {})
    if change == "unanchored":
        await orch.computer_sessions.pause(session, "approval_not_completed")
    else:
        await rc.pause_failed_computer_approval(orch, ws, authority)
    if change == "task":
        hc.begin_task(orch, ws, OWNER, CHAT)
    elif change == "chat":
        orch._ws_active_chat[id(ws)] = "other"
    elif change == "generation":
        session.authority_generation += 1
    elif change == "manual_pause":
        await orch.computer_sessions.pause(session, "manual_pause")
    assert rc.capture_computer_approval(orch, ws, OWNER, CHAT, {}) is None


async def test_bypass_recovers_only_a_bound_server_pause_without_a_card(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _bypassed(task_runtime)
    authority = rc.capture_computer_approval(orch, ws, OWNER, CHAT, {})
    await rc.pause_failed_computer_approval(orch, ws, authority)
    response = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    assert response.error is None and len(effects) == 1 and session.state == "active"
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


async def test_stale_remote_decision_retires_the_card_and_audits_failure(task_runtime, monkeypatch):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _mac(task_runtime, remote=True)
    response = await _call(task_runtime, "run_command", argv=["ls"], cwd=ROOT)
    replacement = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(rc, "_replace_card", replacement)
    monkeypatch.setattr(rc, "_audit_async", audit)
    await orch.computer_sessions.pause(session, "manual_pause")
    await rc.handle_decision(orch, ws, OWNER, _payload(response, "approve"))
    assert not effects and not rc._PENDING_CARDS
    assert replacement.await_args.args[2] == "Expired"
    assert audit.await_args.args[1] == "remote_op.decision_refused"


@pytest.mark.parametrize("metadata,transition,outcome", [
    ({"effect_state": "not_started"}, "effect_failed_no_effect", "failure"),
    ({}, "effect_uncertain", "interrupted"),
    ({"effect_state": "uncertain"}, "effect_uncertain", "interrupted"),
])
async def test_typed_host_no_effect_is_distinct_from_untyped_failure(task_runtime, metadata, transition, outcome):
    from shared.protocol import MCPResponse

    orch, _ws, effects, *_rest, result = task_runtime
    await _bypassed(task_runtime)
    result[0] = MCPResponse(error={"code": "permission_denied", "message": "refused", **metadata})
    await _call(task_runtime, "type_text", text="hello", hwnd=42)
    events = [call.args[0] for call in orch.audit_recorder.record.await_args_list]
    event = next(event for event in events if event.action_type == "hitl." + transition)
    assert event.outcome == outcome and len(effects) == 1


async def test_bypass_terminal_input_uses_exact_single_use_permit(task_runtime):
    from orchestrator import hitl_confirmation as hc
    from shared.protocol import MCPResponse

    orch, ws, effects, *_ = task_runtime
    session, _host = await _bypassed(task_runtime)
    permits = []

    async def execute(websocket, agent, tool, args, **_kwargs):
        async def effect(_capabilities):
            permits.append(hc.consume_terminal_input(orch, session, tool, {**args, "text": "changed"}))
            permits.append(hc.consume_terminal_input(orch, session, tool, args))
            permits.append(hc.consume_terminal_input(orch, session, tool, args))
            effects.append((tool, args))
            return MCPResponse(result={"_data": {"ok": True}})
        return await orch._execute_governed_attempt(websocket, agent, tool, args,
            user_id=OWNER, channel="web", audit_correlation_id="test", actor_user_id=OWNER,
            auth_principal=OWNER, conversation_id=CHAT, invoke=effect)

    orch._execute_with_retry = execute
    response = await _call(task_runtime, "type_text", text="ls", hwnd=53, terminal_app="Terminal")
    assert response.error is None and permits == [False, True, False] and len(effects) == 1
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


async def test_bypass_off_between_consumption_and_effect_refuses_without_card(task_runtime):

    orch, ws, effects, *_ = task_runtime
    session, host = await _bypassed(task_runtime)
    original = orch.audit_recorder.record.side_effect

    async def revoke(event):
        if event.action_type == "hitl.consumed":
            await orch.computer_sessions.set_bypass(host, False)
        return object()

    orch.audit_recorder.record.side_effect = revoke
    response = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    orch.audit_recorder.record.side_effect = original
    assert response.error and not effects
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


@pytest.mark.parametrize("args", [
    {"text": "ls", "hwnd": 53},
    {"text": "ls", "hwnd": 42, "terminal_app": "Terminal"},
    {"text": "ls", "hwnd": 53, "terminal_app": "TextEdit"},
])
async def test_terminal_bypass_requires_a_matching_explicit_terminal_target(task_runtime, args):
    orch, _ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    response = await _call(task_runtime, "type_text", **args)
    assert response.error and not effects
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


@pytest.mark.parametrize("tool,args", [("resume_session", {}), ("type_text", {"text": "hello", "hwnd": 42})])
async def test_bypass_server_resume_remains_bound_to_its_original_chat(task_runtime, tool, args):
    orch, ws, effects, *_ = task_runtime
    session, _host = await _bypassed(task_runtime)
    session.chat_id = "other-chat"
    response = await _call(task_runtime, tool, **args)
    assert response.error and not effects
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


async def test_bypass_revocation_before_recursive_dispatch_never_creates_review(task_runtime):
    orch, _ws, effects, *_ = task_runtime
    _session, host = await _bypassed(task_runtime)

    async def revoke(event):
        if event.action_type == "hitl.bypass":
            await orch.computer_sessions.set_bypass(host, False)
        return object()

    orch.audit_recorder.record.side_effect = revoke
    response = await _call(task_runtime, "run_command", argv=["ls"], cwd=ROOT)
    assert response.error and not effects
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


@pytest.mark.parametrize("tool", ["write_file", "delete_path"])
async def test_single_file_review_discloses_source_risks_and_exact_arguments(task_runtime, monkeypatch, tool):
    orch, _ws, effects, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, tool: policy.SCOPES[tool]}
    monkeypatch.setattr(orch.computer_hosts, "request", AsyncMock(return_value={"text": "old"}))
    args = {"path": ROOT + "/hello.md", **({"content": "new", "if_exists": "refuse"} if tool == "write_file" else {})}
    response = await _call(task_runtime, tool, **args)
    parts = response.ui_components[0]["content"]
    previews = [part for part in parts if part.get("language") == "json"]
    assert len(previews) == 1 and json.loads(previews[0]["code"]) == args
    text = " ".join(part.get("content", "") for part in parts)
    assert "source_bound" in text and "Source:" in text and "tools:files" in text
    assert not effects


def test_multiple_roots_require_an_explicit_primary_for_relative_paths():
    roots = [{"name": "x", "path": ROOT}, {"name": "other", "path": "/Users/sam/Other"}]
    assert policy.resolve_workspace_path("hello.md", roots, "Mac", primary=ROOT) == ROOT + "/hello.md"
    with pytest.raises(policy.CwdRefusal, match="absolute path"):
        policy.resolve_workspace_path("hello.md", roots, "Mac")


async def test_no_session_never_creates_a_dead_file_card(task_runtime):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": policy.SCOPES["write_file"]}
    await orch.computer_sessions.end(session, "user_stop", cancel_turn=False)
    response = await _call(task_runtime, "write_file", path=ROOT + "/notes.md", content="safe")
    assert response.error and not effects and not rc._PENDING_CARDS


async def test_sensitive_file_arguments_are_refused_before_creating_a_review(task_runtime):
    orch, _ws, effects, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": policy.SCOPES["write_file"]}
    response = await _call(task_runtime, "write_file", path=ROOT + "/notes.md", content="password: synthetic-secret")
    assert response.error and "sensitive" in response.error["message"]
    assert not effects and not rc._PENDING_CARDS


async def test_unknown_computer_file_request_never_creates_a_card(task_runtime):
    orch, _ws, effects, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": policy.SCOPES["write_file"]}
    response = await _call(task_runtime, "write_file", computer="missing-mac", path=ROOT + "/notes.md", content="safe")
    assert response.error and "computer_unavailable" in response.error["message"]
    assert not effects and not rc._PENDING_CARDS


@pytest.mark.parametrize("reason", ["local_input", "local_pause", "manual_pause", "approval_not_completed"])
async def test_bypass_never_resumes_human_or_unanchored_pauses(task_runtime, reason):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await _bypassed(task_runtime)
    await orch.computer_sessions.pause(session, reason)
    response = await _call(task_runtime, "run_command", argv=["ls"], cwd=ROOT)
    assert response.error and not effects and session.state == "paused"
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


async def test_server_pause_without_a_task_origin_cannot_recover(task_runtime):
    orch, ws, *_ = task_runtime
    session, _host = await _mac(task_runtime, remote=True)
    orch._hitl_task_origins.clear()
    authority = rc.capture_computer_approval(orch, ws, OWNER, CHAT, {})
    await rc.pause_failed_computer_approval(orch, ws, authority)
    assert session.state == "paused"
    assert rc.capture_computer_approval(orch, ws, OWNER, CHAT, {}) is None


async def test_bypass_task_change_during_server_resume_is_refused_without_review(task_runtime, monkeypatch):
    from orchestrator import hitl_confirmation as hc

    orch, ws, effects, *_ = task_runtime
    session, _host = await _bypassed(task_runtime)
    authority = rc.capture_computer_approval(orch, ws, OWNER, CHAT, {})
    await rc.pause_failed_computer_approval(orch, ws, authority)
    resume = orch.computer_sessions.resume

    async def changed(*args, **kwargs):
        await resume(*args, **kwargs)
        hc.begin_task(orch, ws, OWNER, CHAT)

    monkeypatch.setattr(orch.computer_sessions, "resume", changed)
    response = await _call(task_runtime, "type_text", text="hello", hwnd=42)
    assert response.error and not effects
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


@pytest.mark.parametrize("reason", ["local_input", "local_pause", "manual_pause", "approval_not_completed"])
async def test_review_mode_resume_cannot_offer_a_card_for_human_or_unanchored_pause(task_runtime, reason):
    orch, _ws, effects, *_ = task_runtime
    session, _host = await _mac(task_runtime, remote=True)
    await orch.computer_sessions.pause(session, reason)
    response = await _call(task_runtime, "resume_session")
    assert response.error and not effects and session.state == "paused"
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


async def test_review_mode_server_resume_is_bound_and_explicit(task_runtime):
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=True)
    host.websocket = ws
    authority = rc.capture_computer_approval(orch, ws, OWNER, CHAT, {})
    await rc.pause_failed_computer_approval(orch, ws, authority)
    response = await _call(task_runtime, "resume_session")
    from orchestrator import hitl_confirmation as hc
    payload = next(part["payload"] for part in response.ui_components[0]["content"]
                   if part.get("payload", {}).get("decision") == "approve_and_resume")
    await hc.handle_decision(orch, ws, OWNER, payload)
    assert session.state == "active" and [tool for tool, _args in effects] == ["resume_session"]


@pytest.mark.parametrize("bypass", [False, True])
async def test_computer_discovery_is_available_without_review_in_both_modes(task_runtime, bypass):
    orch, _ws, effects, *_ = task_runtime
    if bypass:
        await _bypassed(task_runtime)
    else:
        await _mac(task_runtime)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "list_computers": "tools:read"}
    response = await _call(task_runtime, "list_computers")
    assert response.error is None and [tool for tool, _args in effects] == ["list_computers"]
    assert not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


@pytest.mark.parametrize("bypass", [False, True])
async def test_rejected_browser_url_is_an_error_without_a_waiting_state(task_runtime, bypass):
    orch, _ws, effects, *_ = task_runtime
    if bypass:
        await _bypassed(task_runtime)
    else:
        await _mac(task_runtime, remote=True)
    response = await _call(task_runtime, "browser_navigate", url="file:///tmp/notes.md")
    assert response.error and "url_rejected" in response.error["message"]
    assert "confirmation_required" not in json.dumps(response.result)
    assert not effects and not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


async def test_invalid_review_marker_is_an_error_in_bypass(task_runtime):
    orch, _ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    response = await _call(task_runtime, "run_command", argv=["ls"], cwd=ROOT, _remote_op_proposal_id="expired")
    assert response.error and "no longer valid" in response.error["message"]
    assert "confirmation_required" not in json.dumps(response.result)
    assert not effects and not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})


async def test_invalid_previously_reviewed_operation_is_an_error_in_bypass(task_runtime, monkeypatch):
    from orchestrator import hitl_confirmation as hc

    orch, _ws, effects, *_ = task_runtime
    await _bypassed(task_runtime)
    monkeypatch.setattr(hc, "reviewed_remote_proposal", lambda *_args: "expired")
    response = await _call(task_runtime, "run_command", argv=["ls"], cwd=ROOT)
    assert response.error and "expired or changed" in response.error["message"]
    assert "confirmation_required" not in json.dumps(response.result)
    assert not effects and not rc._PENDING_CARDS and not getattr(orch, "_hitl_pending_calls", {})
