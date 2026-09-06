"""Exercise the host's real lifecycle and refusal methods at external seams."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from orchestrator.orchestrator import Orchestrator
from persistent_agents.dispatch_context import DispatchDenied, bind_dispatch
from persistent_agents.tests.test_dispatch_integration import context
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.test_chain_authority import _mta
from tests.test_chain_hop import _parent, _register_parent, _run_hop
from tests.test_chain_hop import orch as shared_hop_host

hop_host = shared_hop_host


class StartupObserved(Exception):
    """Stop before HTTP serving, after real background-owner wiring."""


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_actual_startup_only_starts_explicitly_enabled_assignment_runner(monkeypatch, enabled):
    from orchestrator import session_store
    from persistent_agents import approvals, runner, service
    monkeypatch.setattr(session_store, "assert_production_posture", Mock())
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "persistent_agents" and enabled)
    created_service = SimpleNamespace(approval_executor=None)
    created_runner = SimpleNamespace(start=Mock())
    factory = Mock(return_value=created_service)
    runner_factory = Mock(return_value=created_runner)
    bridge = Mock(return_value=object())
    monkeypatch.setattr(service, "AssignmentService", factory)
    monkeypatch.setattr(runner, "AssignmentRunner", runner_factory)
    monkeypatch.setattr(approvals, "AssignmentApprovalBridge", bridge)

    def background(coroutine, *, name):
        coroutine.close()
        if name == "generated-agent-relaunch":
            raise StartupObserved

    hub = SimpleNamespace(runtime_composition=SimpleNamespace(start=Mock()),
        generated_agent_publication_service=SimpleNamespace(
            recover_once=AsyncMock(return_value=SimpleNamespace(degraded_publication_ids=())), start=Mock()),
        _track_startup_background_task=background, _jwks_warm_loop=AsyncMock(),
        _personal_agent_watchdog_task=SimpleNamespace(done=lambda: False),
        _start_phi_warm=Mock(), _monitor_agents=AsyncMock(),
        lifecycle_manager=SimpleNamespace(reconcile_orphaned_draft_permissions=Mock(return_value=0),
            reconcile_legacy_directory_ownership=Mock()))
    with pytest.raises(StartupObserved):
        await Orchestrator._run_started_server(hub)
    assert hub._scheduler_loop is None
    if enabled:
        factory.assert_called_once_with(hub)
        runner_factory.assert_called_once_with(hub, created_service)
        bridge.assert_called_once_with(created_runner)
        assert created_service.approval_executor is bridge.return_value
        created_runner.start.assert_called_once()
    else:
        factory.assert_not_called()
        runner_factory.assert_not_called()
        assert hub.persistent_assignment_runner is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_shutdown_awaits_assignment_runner_before_store_and_plane_shutdown(monkeypatch, failure):
    from orchestrator import orchestrator as module
    events = []
    async def stop():
        events.append("runner_stopped")
        if failure:
            raise RuntimeError("runner shutdown failure")
    hub = Orchestrator.__new__(Orchestrator)
    hub._startup_background_tasks = set()
    hub.persistent_assignment_runner = SimpleNamespace(stop=stop)
    hub.persistent_assignments = SimpleNamespace(store=SimpleNamespace(close=lambda: events.append("store_closed")))
    hub.async_task_manager = SimpleNamespace(drain=AsyncMock(return_value=0), stop_retention_sweep=AsyncMock())
    monkeypatch.setattr(module, "_unbind_orchestrator_process_consumers", lambda _: events.append("unbound"))
    if failure:
        with pytest.raises(RuntimeError, match="runner shutdown failure"):
            await hub._close_started_services_once()
    else:
        await hub._close_started_services_once()
    assert events[:2] == ["runner_stopped", "store_closed"]
    assert "unbound" in events


@pytest.mark.asyncio
async def test_assignment_never_uses_unrelated_latest_offline_grant():
    authority, _, grants = _mta(latest="unrelated")
    denied = await authority.derive(user_id="u1", agent_id=None, consented_scopes=["tools:read"],
                                    grant_id=None, turn_class="persistent_assignment")
    assert denied.reason == "missing_consent"
    grants.latest_valid_for.assert_not_called()
    admitted = await authority.derive(user_id="u1", agent_id="a1", consented_scopes=["tools:read"],
                                      grant_id="g1", turn_class="persistent_assignment")
    assert admitted.principal == "machine:persistent_assignment"


@pytest.mark.asyncio
async def test_dispatch_cannot_retry_fan_out_or_publish_uncheckpointed_ui():
    hub = Orchestrator.__new__(Orchestrator)
    hub.MAX_RETRIES = 5
    hub.execute_tool_and_wait = AsyncMock(return_value=MCPResponse(error={"retryable": True}))
    hub._protected_dispatch_channel = lambda *a, **kw: "persistent_assignment"
    with bind_dispatch(context()):
        await hub._execute_with_retry(None, "reader", "read", {})
        with pytest.raises(DispatchDenied, match="unreserved_parallel"):
            await hub.execute_parallel_tools(None, [], {})
        # An uninitialized renderer would raise if any transient frame reached it.
        assert await hub.send_ui_render(None, [{"type": "text", "text": "uncommitted"}]) is None
    hub.execute_tool_and_wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_initiated_hop_cannot_escape_durable_child_admission(hop_host, monkeypatch):
    monkeypatch.setitem(flags._flags, "recursive_delegation", True)
    with bind_dispatch(context()):
        _register_parent(hop_host, _parent())
    assert hop_host._dispatch_context["req-parent"]["persistent_assignment"]
    response = await _run_hop(hop_host)
    assert "durable task reservation" in response.error["message"]
    assert not hop_host._dispatched


@pytest.mark.asyncio
async def test_narrow_chat_still_offers_owner_controls_without_text_only_prohibition(monkeypatch):
    from orchestrator import chat_steps, turn_hooks
    from orchestrator.orchestrator import TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "persistent_agents")
    monkeypatch.setattr(chat_steps, "ChatStepRecorder", Mock(return_value=SimpleNamespace(finish=AsyncMock())))
    monkeypatch.setattr(turn_hooks, "flow_pattern", Mock(return_value=None))
    monkeypatch.setattr(turn_hooks, "new_ledger", Mock(return_value=None))
    monkeypatch.setattr(turn_hooks, "match_skill", Mock(return_value=None))
    hub = Orchestrator.__new__(Orchestrator)
    socket = Mock(task=None)
    hub.ui_sessions = {socket: {"sub": "owner"}}
    hub.history = SimpleNamespace(get_chat_agent=Mock(return_value=None), get_chat=Mock(return_value=None),
                                  get_file_mappings=Mock(return_value=[]))
    hub.workspace = SimpleNamespace(alive_rows=AsyncMock(return_value=[]))
    hub.tool_permissions = SimpleNamespace(list_disabled_agents=Mock(return_value=[]))
    hub._append_conversation_message = AsyncMock(return_value=None)
    hub._notify_phi_if_detected = AsyncMock()
    hub._safe_send = AsyncMock()
    hub.send_ui_render = AsyncMock()
    hub._chat_recorders = {}
    hub._chain_budgets = {}
    hub._active_request = {}
    hub.cancelled_sessions = {}
    hub.agent_cards = {}
    hub._skill_store = Mock(return_value=None)
    hub._start_heartbeat = AsyncMock(return_value=SimpleNamespace(cancel=Mock()))
    hub._call_llm = AsyncMock(side_effect=asyncio.CancelledError)
    hub.runtime_composition = SimpleNamespace(plane=SimpleNamespace(runtime=object(), repositories=object()))
    hub.compute_tools_available_for_user = Mock(return_value=False)
    with pytest.raises(asyncio.CancelledError):
        await hub._handle_chat_message_impl(socket, "List my ongoing agents", "chat", user_id="owner",
                                            llm_preflight_complete=True)
    call = hub._call_llm.call_args
    assert call is not None
    messages = call.args[1]
    assert "ONGOING AGENTS" in messages[0]["content"]
    assert TEXT_ONLY_SYSTEM_PROMPT_ADDENDUM not in messages[0]["content"]
    definitions = call.kwargs.get("tools_desc") or call.args[2]
    assert [item["function"]["name"] for item in definitions] == ["ongoing_agent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_chat_routes_exact_owner_command_to_control_service(monkeypatch, parallel):
    from persistent_agents import chat_tools
    handler = AsyncMock(return_value=MCPResponse(result={"assignments": []}))
    monkeypatch.setattr(chat_tools, "handle_meta_tool", handler)
    hub = Orchestrator.__new__(Orchestrator)
    hub._map_file_paths = lambda chat, args, **kw: args
    tool = SimpleNamespace(function=SimpleNamespace(name="ongoing_agent", arguments='{"command":"list"}'))
    mapping = {"ongoing_agent": chat_tools.META_AGENT_ID}
    socket = object()
    if parallel:
        results = await hub.execute_parallel_tools(socket, [tool], mapping, chat_id="chat", user_id="owner")
        assert results[0].result == {"assignments": []}
    else:
        result = await hub.execute_single_tool(socket, tool, mapping, chat_id="chat", user_id="owner")
        assert result.result == {"assignments": []}
    handler.assert_awaited_once_with(hub, "ongoing_agent", {"command": "list"},
                                    user_id="owner", chat_id="chat", websocket=socket)
