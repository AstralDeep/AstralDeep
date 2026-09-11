"""Public research and attended decisions through the actual shared dispatcher."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import hitl, hitl_confirmation as hc
from shared.protocol import MCPResponse
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401


READS = [
    ("web-research-1", "web_search", {"query": "weather Lexington KY", "max_results": 8}),
    ("web-research-1", "research_brief", {"topic": "recent space research", "depth": "standard"}),
    ("web-research-1", "fetch_page", {"url": "https://www.nasa.gov/news"}),
    ("general-1", "search_arxiv", {"query": "cat:cs.AI", "max_results": 10}),
    ("summarizer-1", "summarize_url", {"url": "https://arxiv.org/abs/2401.12345"}),
]


def _register(orch, agent, tool):
    package, name, *_ = hitl._PUBLIC_READS[(agent, tool)]
    cls = getattr(import_module(f"agents.{package}.{package}_agent"), name)
    instance = cls.__new__(cls)
    instance.mcp_server = SimpleNamespace(tools=import_module(f"agents.{package}.mcp_tools").TOOL_REGISTRY)
    orch.local_agents[agent] = instance
    return instance


@pytest.fixture
def runtime(gate_orchestrator, monkeypatch):  # noqa: F811 - pytest fixture import
    orch, ws = gate_orchestrator
    monkeypatch.setattr(hitl, "hitl_enabled", lambda: True)
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate",
                        lambda: SimpleNamespace(contains_phi=lambda _text: False))
    orch._active_request = {"chat-a": "Research the public web for this question"}
    orch._ws_active_chat = {id(ws): "chat-a"}
    orch._is_user_agent = lambda _id: False
    orch.send_ui_render = AsyncMock()
    orch.audit_recorder = SimpleNamespace(record=AsyncMock())
    orch.history = SimpleNamespace(get_chat=MagicMock(return_value={"id": "chat-a"}))
    orch._llm_context_user_id = lambda _ws: "owner-a"
    orch._llm_store = SimpleNamespace(get=AsyncMock(return_value=None))
    orch._execute_with_retry = AsyncMock(return_value=MCPResponse(result={"_data": {"ok": True}}))
    orch.workspace = SimpleNamespace(aupsert=AsyncMock(return_value=[{"op": "upsert"}]))
    orch.send_ui_upsert = AsyncMock()

    async def mutate(**kwargs):
        return await kwargs["mutation"]()
    orch.run_detached_conversation_mutation = mutate
    return orch, ws


async def _dispatch(runtime, tool="send_email", args=None, agent="agent-a"):
    orch, ws = runtime
    tc = SimpleNamespace(id="test", function=SimpleNamespace(name=tool, arguments=json.dumps(args or {})))
    return await orch.execute_single_tool(ws, tc, {tool: agent}, "chat-a", user_id="owner-a")


def _decision(response, decision="approve"):
    assert response.error is None
    assert response.result["_data"]["status"] == "confirmation_required"
    buttons = [item for item in response.ui_components[0]["content"] if item["type"] == "button"]
    assert [button["label"] for button in buttons] == ["Approve", "Decline"]
    assert all(button["action"] == "authorize_action" for button in buttons)
    return next(button["payload"] for button in buttons if button["payload"]["decision"] == decision)


@pytest.mark.parametrize("agent,tool,args", READS)
async def test_registered_public_research_runs_without_second_confirmation(runtime, agent, tool, args):
    orch, _ = runtime
    _register(orch, agent, tool)
    response = await _dispatch(runtime, tool, args, agent)
    assert response.error is None and response.result["_data"]["ok"]
    orch._execute_with_retry.assert_awaited_once()
    assert not getattr(orch, "_hitl_pending_calls", {})


async def test_fetched_untrusted_public_url_still_reads(runtime, monkeypatch):
    from orchestrator import taint
    orch, _ = runtime
    _register(orch, "web-research-1", "fetch_page")
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    tracker = MagicMock()
    tracker.effective_trust_of_args.return_value = taint.UNTRUSTED
    orch._taint_tracker = lambda _chat: tracker
    response = await _dispatch(runtime, "fetch_page", {"url": "https://www.nasa.gov/news"}, "web-research-1")
    assert not response.error
    orch._execute_with_retry.assert_awaited_once()


@pytest.mark.parametrize("args", [
    {"url": "https://example.org", "headers": {"Authorization": "secret"}},
    {"url": "https://example.org", "body": "private record"},
    {"url": "https://alice:pass@example.org"},
    {"url": "https://example.org/?token=secret"},
    {"url": "https://example.org/?token=abcdef"},
    {"url": "https://example.org/?signature=abcdef"},
    {"url": "https://example.org/?key=abcdef"},
    {"url": "https://example.org/?code=abcdef"},
    {"url": "https://example.org/?api_key=abc"},
    {"url": "https://example.org/%2561pi_key=abc"},
    {"url": "http://127.0.0.1"}, {"url": "http://169.254.169.254"},
    {"url": "http://10.0.0.1"}, {"url": "http://[::1]"},
    {"url": "http://localhost"}, {"url": "https://hospital.internal"},
    {"url": "file:///tmp/report"}, {"url": "https://example.org:abc"},
    {"url": "https://example.org\nprivate"}, {"url": "https://example.org#private"},
    {"url": "https://example.org", "_credentials": "private"},
])
async def test_private_or_unknown_fetch_arguments_are_held(runtime, args):
    orch, _ = runtime
    _register(orch, "web-research-1", "fetch_page")
    response = await _dispatch(runtime, "fetch_page", args, "web-research-1")
    if response.error:
        assert "Review unavailable" in response.error["message"]
        assert response.ui_components is None
        assert not getattr(orch, "_hitl_pending_calls", {})
    else:
        payload = _decision(response)
        assert set(payload) == {"hitl_request_id", "decision"}
    orch._execute_with_retry.assert_not_awaited()


@pytest.mark.parametrize("query", ["MRN 123456 diabetes", "SSN 123-45-6789", "patient Alice's diagnosis",
                                    "password: abc", "bearer abcd", "alice@example.com"])
async def test_sensitive_search_queries_are_not_public_authority(runtime, query):
    orch, _ = runtime
    _register(orch, "web-research-1", "web_search")
    response = await _dispatch(runtime, "web_search", {"query": query}, "web-research-1")
    assert "Review unavailable" in response.error["message"]
    orch._execute_with_retry.assert_not_awaited()


@pytest.mark.parametrize("agent,tool,args", READS)
def test_remote_names_cannot_claim_first_party_contract(runtime, agent, tool, args):
    orch, _ = runtime
    orch.local_agents[agent] = SimpleNamespace(mcp_server=SimpleNamespace(tools={tool: {"scope": "tools:read"}}))
    assert not hitl.registered_public_reader(orch, agent, tool)
    assert hitl.assess_risk(tool, args, agent_id=agent) == [hitl.EGRESS]


def test_registry_write_scope_and_replaced_function_cannot_opt_in(runtime):
    orch, _ = runtime
    instance = _register(orch, "web-research-1", "fetch_page")
    real = instance.mcp_server.tools["fetch_page"]
    instance.mcp_server = SimpleNamespace(tools={"fetch_page": {**real, "scope": "tools:write"}})
    assert not hitl.registered_public_reader(orch, "web-research-1", "fetch_page")
    instance.mcp_server.tools["fetch_page"] = {**real, "function": lambda: None}
    assert not hitl.registered_public_reader(orch, "web-research-1", "fetch_page")
    assert not hitl.registered_public_reader(orch, "other", "read")
    instance.mcp_server.tools.clear()
    assert not hitl.registered_public_reader(orch, "web-research-1", "fetch_page")


async def test_approval_runs_exact_stored_call_once_and_retires_card(runtime):
    orch, ws = runtime
    args = {"recipient": "test@example.org", "body": "authorized message"}
    payload = _decision(await _dispatch(runtime, args=args))
    orch._execute_with_retry.assert_not_awaited()
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_awaited_once()
    dispatched = orch._execute_with_retry.await_args.args[3]
    assert {key: value for key, value in dispatched.items() if not key.startswith("_")} == args
    assert hc._APPROVAL.get() is None
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_awaited_once()
    assert "unavailable" in orch.send_ui_render.await_args.args[1][0]["message"]
    assert orch.send_ui_upsert.await_count == 1


async def test_decline_never_dispatches(runtime):
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime), "decline")
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_not_awaited()
    assert "Declined" in orch.send_ui_render.await_args.args[1][0]["message"]


@pytest.mark.parametrize("case", ["owner", "chat", "extra_args", "invalid_decision", "unattended", "expired", "restart"])
async def test_invalid_clicks_never_dispatch(runtime, case):
    orch, ws = runtime
    payload = dict(_decision(await _dispatch(runtime)))
    owner = "owner-a"
    if case == "owner":
        owner = "other"
    elif case == "chat":
        orch._ws_active_chat[id(ws)] = "other-chat"
    elif case == "extra_args":
        payload["args"] = {"body": "different"}
    elif case == "invalid_decision":
        payload["decision"] = "yes"
    elif case == "unattended":
        orch.ui_sessions[ws]["machine_class"] = "scheduled"
    elif case == "expired":
        key = payload["hitl_request_id"]
        orch._hitl_pending_calls[key] = replace(orch._hitl_pending_calls[key], expires=0)
    else:
        orch._hitl_pending_calls.clear()
    await hc.handle_decision(orch, ws, owner, payload)
    orch._execute_with_retry.assert_not_awaited()


async def test_duplicate_click_race_and_revoked_permissions_fail_closed(runtime):
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    orch.tool_permissions.is_tool_allowed.return_value = False
    await asyncio.gather(*(hc.handle_decision(orch, ws, "owner-a", payload) for _ in range(2)))
    orch._execute_with_retry.assert_not_awaited()
    assert not orch._hitl_pending_calls


async def test_approval_does_not_override_phi_hook_or_security_block(runtime, monkeypatch):
    from shared.feature_flags import flags
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    monkeypatch.setitem(flags._flags, "hook_system", True)
    orch.hooks.emit.return_value = SimpleNamespace(action="block", reason="PHI policy denied")
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_not_awaited()
    monkeypatch.setitem(flags._flags, "hook_system", False)
    payload = _decision(await _dispatch(runtime))
    orch.security_flags = {"agent-a": {"send_email": {"blocked": True}}}
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_not_awaited()


async def test_taint_sink_remains_denied_after_approval(runtime, monkeypatch):
    from orchestrator import taint
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    tracker = MagicMock()
    tracker.effective_trust_of_args.return_value = taint.UNTRUSTED
    orch._taint_tracker = lambda _chat: tracker
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_not_awaited()


async def test_call_retries_reuse_one_actionable_card(runtime):
    orch, _ = runtime
    first = await _dispatch(runtime)
    second = await _dispatch(runtime)
    assert _decision(first) == _decision(second)
    assert len(orch._hitl_pending_calls) == 1


@pytest.mark.parametrize("case", ["owner_lookup", "dispatch_error", "audit_error", "card_error"])
async def test_failures_are_visible_and_never_reuse_approval(runtime, case):
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    if case == "owner_lookup":
        orch.history.get_chat.side_effect = RuntimeError("database unavailable")
    elif case == "dispatch_error":
        orch.execute_single_tool = AsyncMock(side_effect=RuntimeError("failed"))
    elif case == "audit_error":
        orch.audit_recorder.record.side_effect = RuntimeError("audit unavailable")
    else:
        orch.workspace.aupsert.side_effect = RuntimeError("render failed")
    await hc.handle_decision(orch, ws, "owner-a", payload)
    assert not orch._hitl_pending_calls
    assert hc._APPROVAL.get() is None
    assert orch.send_ui_render.await_count


@pytest.mark.parametrize("changes", [{"tool": "upload_file"}, {"owner": "other"}, {"chat": "other"},
                                     {"agent": "other"}, {"arguments": "{\"body\":\"altered\"}"},
                                     {"risks": (hitl.IRREVERSIBLE,)}])
async def test_approval_binding_and_risks_cannot_be_retargeted(runtime, changes):
    orch, ws = runtime
    response = await _dispatch(runtime)
    call = orch._hitl_pending_calls[_decision(response)["hitl_request_id"]]
    token = hc._APPROVAL.set(hc.Approval(orch, replace(call, **changes)))
    try:
        result = await hc.evaluate(orch, ws, "owner-a", "chat-a", "agent-a", "send_email", {}, [hitl.EGRESS])
        assert result is not None
    finally:
        hc._APPROVAL.reset(token)


async def test_one_context_cannot_authorize_second_or_child_call(runtime):
    orch, ws = runtime
    response = await _dispatch(runtime)
    call = orch._hitl_pending_calls[_decision(response)["hitl_request_id"]]
    token = hc._APPROVAL.set(hc.Approval(orch, call))
    try:
        assert await hc.evaluate(orch, ws, "owner-a", "chat-a", "agent-a", "send_email", {}, [hitl.EGRESS]) is None
        assert await hc.evaluate(orch, ws, "owner-a", "chat-a", "agent-a", "send_email", {}, [hitl.EGRESS]) is not None
    finally:
        hc._APPROVAL.reset(token)


async def test_unattended_and_oversized_confirmations_refuse(runtime):
    orch, ws = runtime
    orch.ui_sessions[ws]["_invocation_channel"] = "mcp"
    assert "interactive" in (await _dispatch(runtime)).error["message"]
    orch.ui_sessions[ws].pop("_invocation_channel")
    response = await _dispatch(runtime, args={"body": "x" * 65537})
    assert "could not be prepared" in response.error["message"]


def test_public_contract_rejects_malformed_options_and_keeps_cross_principal():
    for args in ({}, {"query": ""}, {"query": []}, {"query": "x", "max_results": True},
                 {"query": "x", "max_results": 21}, {"query": "x" * 4097}):
        assert not hitl.public_read_arguments("web-research-1", "web_search", args)
    assert not hitl.public_read_arguments("web-research-1", "research_brief", {"topic": "x", "depth": []})
    assert not hitl.public_read_arguments("other", "fetch_page", {})
    assert not hitl.public_read_arguments("web-research-1", "fetch_page", {"url": "https://private"})
    assert hitl.assess_risk("fetch_page", {"url": "https://example.org"}, agent_id="web-research-1",
        public_reader=True, actor_principal="a", target_principal="b") == [hitl.CROSS_PRINCIPAL]


async def test_actual_ui_event_routes_opaque_id_to_bound_approval(runtime, monkeypatch):
    orch, ws = runtime
    monkeypatch.setattr("audit.hooks.record_ws_action", AsyncMock())
    orch._get_user_id = lambda _ws: "owner-a"
    payload = _decision(await _dispatch(runtime))
    await orch.handle_ui_message(ws, json.dumps({"type": "ui_event", "action": "authorize_action", "payload": payload}))
    orch._execute_with_retry.assert_awaited_once()


async def test_approved_result_components_are_published(runtime):
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    components = [{"type": "text", "content": "Operation completed"}]
    orch._execute_with_retry.return_value = MCPResponse(result={"_data": {"ok": True}}, ui_components=components)
    await hc.handle_decision(orch, ws, "owner-a", payload)
    assert orch.workspace.aupsert.await_args.args[2] == components
    assert orch.send_ui_upsert.await_count == 1


async def test_changed_risks_produce_new_card_and_never_claim_completion(runtime):
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    new_card = {"type": "card", "title": "Further authorization", "content": []}
    orch.execute_single_tool = AsyncMock(return_value=MCPResponse(
        result={"_data": {"status": "confirmation_required"}}, ui_components=[new_card]))
    await hc.handle_decision(orch, ws, "owner-a", payload)
    message = orch.send_ui_render.await_args.args[1][0]["message"]
    assert "still needs approval" in message and "Approval used" not in message
    assert orch.workspace.aupsert.await_args.args[2] == [new_card]


async def test_supervisor_treats_exact_click_as_expressed_intent(runtime, monkeypatch):
    from orchestrator import supervisor
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime, "delete_account"))
    monkeypatch.setattr(supervisor, "supervisor_enabled", lambda: True)
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_awaited_once()


@pytest.mark.parametrize("gate", ["permission", "policy", "phi_hook"])
async def test_public_read_exemption_never_bypasses_other_gates(runtime, monkeypatch, gate):
    from orchestrator import policy
    from shared.feature_flags import flags
    orch, _ = runtime
    _register(orch, "web-research-1", "fetch_page")
    if gate == "permission":
        orch.tool_permissions.is_tool_allowed.return_value = False
    elif gate == "policy":
        monkeypatch.setattr(policy, "policy_enabled", lambda: True)
        monkeypatch.setattr(policy, "load_rules", lambda: [])
        monkeypatch.setattr(policy, "evaluate_policy", lambda *_: policy.PolicyDecision(effect=policy.DENY))
    else:
        monkeypatch.setitem(flags._flags, "hook_system", True)
        orch.hooks.emit.return_value = SimpleNamespace(action="block", reason="PHI detected")
    assert (await _dispatch(runtime, "fetch_page", {"url": "https://example.org"}, "web-research-1")).error
    orch._execute_with_retry.assert_not_awaited()


def test_review_summary_exposes_only_host_and_field_names():
    summary = hc._review_summary({"url": "https://user:pass@example.org/a?key=secret", "body": "confidential"})
    assert "example.org" in summary and "url, body" in summary
    assert "secret" not in summary and "pass" not in summary and "confidential" not in summary
    assert "needs review" in hc._review_summary({"url": "https://[invalid"})


async def test_request_count_is_bounded_and_malformed_click_is_safe(runtime):
    orch, ws = runtime
    for n in range(32):
        _decision(await _dispatch(runtime, args={"body": str(n)}))
    assert "Too many" in (await _dispatch(runtime, args={"body": "33"})).error["message"]
    await hc.handle_decision(orch, ws, "owner-a", {"hitl_request_id": []})
    assert "unavailable" in orch.send_ui_render.await_args.args[1][0]["message"]


def test_virtual_socket_never_counts_as_attended(runtime):
    from orchestrator.async_tasks import VirtualWebSocket
    orch, _ = runtime
    ws = VirtualWebSocket.__new__(VirtualWebSocket)
    orch.ui_sessions[ws] = {"sub": "owner-a"}
    assert not hc._attended(orch, ws, "owner-a")
    assert not hc._attended(orch, None, "owner-a")


@pytest.mark.parametrize("stage", ["history", "approved_audit", "consumed_audit"])
@pytest.mark.parametrize("change", ["owner", "chat", "expiry"])
async def test_session_races_across_awaits_never_dispatch(runtime, monkeypatch, stage, change):
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    original_audit = hc._audit
    now = hc.time.monotonic()

    def mutate_session():
        if change == "owner":
            orch.ui_sessions[ws] = {"sub": "other-owner"}
        elif change == "chat":
            orch._ws_active_chat[id(ws)] = "other-chat"
        else:
            monkeypatch.setattr(hc.time, "monotonic", lambda: now + hc.TTL_SECONDS + 1)

    if stage == "history":
        def lookup(*_args, **_kwargs):
            mutate_session()
            return {"id": "chat-a"}
        orch.history.get_chat.side_effect = lookup
    else:
        async def audit(host, call, transition):
            await original_audit(host, call, transition)
            if transition == stage.removesuffix("_audit"):
                mutate_session()
        monkeypatch.setattr(hc, "_audit", audit)
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_not_awaited()
    assert not orch._hitl_pending_calls


@pytest.mark.parametrize("args", [[], [["url", "https://example.org"]], None, "text", 123])
async def test_malformed_confirmation_args_cannot_leave_orphan_pending(runtime, args):
    orch, ws = runtime
    response = await hc.evaluate(orch, ws, "owner-a", "chat-a", "agent-a", "send_email", args, [hitl.EGRESS])
    assert response.error
    assert not getattr(orch, "_hitl_pending_calls", {})


def test_review_preview_is_complete_and_consequential(runtime):
    args = {"path": "/reports/old-report.txt", "amount": 5, "body": "Please review the draft.",
            "url": "https://example.org/report", "options": [True, "safe"]}
    preview, reviewable = hc._review_arguments(args)
    assert reviewable and json.loads(preview) == args


@pytest.mark.parametrize("args", [{"patient_name": "Private Person"}, {"password": "keep-secret"},
                                  {"body": "x" * 600}, {"url": "https://[invalid"},
                                  {"options": list(range(1500))}])
def test_redacted_or_truncated_preview_cannot_authorize(runtime, args):
    preview, reviewable = hc._review_arguments(args)
    assert not reviewable
    assert "Private Person" not in preview and "keep-secret" not in preview


@pytest.mark.parametrize("failure", [False, True])
async def test_named_phi_and_unavailable_analyzer_refuse_blind_approval(runtime, monkeypatch, failure):
    orch, _ = runtime
    detector = MagicMock()
    if failure:
        detector.contains_phi.side_effect = RuntimeError("unavailable")
    else:
        detector.contains_phi.return_value = True
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate", lambda: detector)
    response = await _dispatch(runtime, args={"body": "Example Person has a diagnosis"})
    assert "Review unavailable" in response.error["message"]
    assert not getattr(orch, "_hitl_pending_calls", {})
    orch._execute_with_retry.assert_not_awaited()


@pytest.mark.parametrize("stage", ["map", "hook"])
async def test_late_session_loss_after_consumption_never_executes(runtime, monkeypatch, stage):
    from shared.feature_flags import flags
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    if stage == "map":
        def mapping(_chat, args, **_kwargs):
            orch.ui_sessions.pop(ws)
            return args
        orch._map_file_paths = mapping
    else:
        monkeypatch.setitem(flags._flags, "hook_system", True)
        async def hook(_ctx):
            orch.ui_sessions.pop(ws)
            return SimpleNamespace(action="allow", modified_args=None)
        orch.hooks.emit.side_effect = hook
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_not_awaited()


async def test_hook_cannot_change_reviewed_recipient_or_body(runtime, monkeypatch):
    from shared.feature_flags import flags
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime, args={"recipient": "bob@example.org", "body": "Hello"}))
    monkeypatch.setitem(flags._flags, "hook_system", True)
    orch.hooks.emit.return_value = SimpleNamespace(action="modify",
        modified_args={"recipient": "other@example.org", "body": "Different"})
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch._execute_with_retry.assert_not_awaited()


async def test_one_approval_never_retries_an_uncertain_send(runtime):
    from orchestrator.orchestrator import Orchestrator
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    orch._execute_with_retry = Orchestrator._execute_with_retry.__get__(orch)
    orch.execute_tool_and_wait = AsyncMock(side_effect=[
        MCPResponse(error={"message": "timeout", "retryable": True}), MCPResponse(result={"ok": True})])
    await hc.handle_decision(orch, ws, "owner-a", payload)
    orch.execute_tool_and_wait.assert_awaited_once()


@pytest.mark.parametrize("change", ["owner", "chat", "args", "new_private_field", "none"])
async def test_physical_effect_boundary_revalidates_after_governed_await(runtime, change):
    orch, ws = runtime
    payload = _decision(await _dispatch(runtime))
    call = orch._hitl_pending_calls[payload["hitl_request_id"]]
    args = {"_delegation_token": "trusted-system-context", "user_id": "owner-a", "session_id": "chat-a"}
    physical = AsyncMock(return_value=MCPResponse(result={"ok": True}))

    async def authorize(**kwargs):
        if change == "owner":
            orch.ui_sessions[ws] = {"sub": "other"}
        elif change == "chat":
            orch._ws_active_chat[id(ws)] = "other"
        elif change == "args":
            args["recipient"] = "different"
        elif change == "new_private_field":
            args["_unknown_data"] = "hidden extra payload"
        return await kwargs["invoke"]({})

    orch.governed_final_dispatch = SimpleNamespace(mode="off", execute=authorize)
    token = hc._APPROVAL.set(hc.Approval(orch, call, consumed=True))
    try:
        response = await orch._execute_governed_attempt(ws, "agent-a", "send_email", args,
            user_id="owner-a", conversation_id="chat-a", channel="interactive",
            audit_correlation_id=None, actor_user_id="owner-a", auth_principal="owner-a", invoke=physical)
        assert bool(response.error) == (change != "none")
        assert physical.await_count == (1 if change == "none" else 0)
    finally:
        hc._APPROVAL.reset(token)


async def test_transport_fallback_cannot_duplicate_an_approved_effect(runtime):
    orch, ws = runtime
    response = await _dispatch(runtime)
    call = orch._hitl_pending_calls[_decision(response)["hitl_request_id"]]
    orch.a2a_clients["agent-a"] = object()
    orch._execute_via_websocket = AsyncMock(return_value=MCPResponse(error={"message": "timeout", "retryable": True}))
    orch._execute_via_a2a = AsyncMock(return_value=MCPResponse(result={"ok": True}))

    async def authorize(**kwargs):
        return await kwargs["invoke"]({})
    orch.governed_final_dispatch = SimpleNamespace(mode="off", execute=authorize)
    token = hc._APPROVAL.set(hc.Approval(orch, call, consumed=True))
    try:
        result = await orch._dispatch_tool_call("agent-a", "send_email", {}, 30, ws,
                                               protected_owner_id="owner-a", protected_conversation_id="chat-a")
        assert "already used" in result.error["message"]
        orch._execute_via_websocket.assert_awaited_once()
        orch._execute_via_a2a.assert_not_awaited()
    finally:
        hc._APPROVAL.reset(token)


def test_explicit_recipient_addresses_are_reviewable(runtime):
    args = {"to": ["alice@example.org", "bob@example.org"], "body": "Please review the plan."}
    preview, ok = hc._review_arguments(args)
    assert ok and json.loads(preview) == args


@pytest.mark.parametrize("args", [{"recipient": {"patient_name": "Private Person", "medical_record": "123456"}},
                                  {"destination": "Patient Private Person MRN 12345"},
                                  {"phone": "123-45-6789"}])
def test_destination_label_cannot_hide_private_payload(runtime, args):
    preview, ok = hc._review_arguments(args)
    assert not ok and "Private Person" not in preview and "123-45-6789" not in preview


async def test_owner_file_alias_is_resolved_before_review_and_runs_same_target(runtime):
    orch, ws = runtime
    def resolve(_chat, args, **_kwargs):
        return {**args, "path": "/owner/reports/old.txt" if args.get("path") == "attachment-1" else args.get("path")}
    orch._map_file_paths = resolve
    response = await _dispatch(runtime, "delete_file", {"path": "attachment-1"})
    assert "/owner/reports/old.txt" in str(response.ui_components)
    await hc.handle_decision(orch, ws, "owner-a", _decision(response))
    orch._execute_with_retry.assert_awaited_once()
    assert orch._execute_with_retry.await_args.args[3]["path"] == "/owner/reports/old.txt"


async def test_target_resolution_failure_never_creates_unreviewable_request(runtime):
    orch, _ = runtime
    orch._map_file_paths = MagicMock(side_effect=RuntimeError("unavailable"))
    response = await _dispatch(runtime, "delete_file", {"path": "attachment-1"})
    assert "targets could not be resolved" in response.error["message"]
    assert not getattr(orch, "_hitl_pending_calls", {})


@pytest.mark.parametrize("stage", ["preview", "proposed_audit"])
@pytest.mark.parametrize("change", ["owner", "chat"])
async def test_preview_never_returns_to_changed_session(runtime, monkeypatch, stage, change):
    orch, ws = runtime
    def mutate():
        if change == "owner":
            orch.ui_sessions[ws] = {"sub": "other"}
        else:
            orch._ws_active_chat[id(ws)] = "other-chat"
    if stage == "preview":
        def preview(_args):
            mutate()
            return "{}", True
        monkeypatch.setattr(hc, "_review_arguments", preview)
    else:
        async def audit(_orch, _call, transition):
            if transition == "proposed":
                mutate()
        monkeypatch.setattr(hc, "_audit", audit)
    response = await _dispatch(runtime)
    assert response.error and response.ui_components is None
    assert not getattr(orch, "_hitl_pending_calls", {})
