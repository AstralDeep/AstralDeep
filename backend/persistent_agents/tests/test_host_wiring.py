"""Tests for the orchestrator host's real lifecycle wiring: the assignment runner starts
only when explicitly enabled and stops before HTTP serving, dispatch cannot retry
or publish uncheckpointed UI, and chat tool calls reach the owner control service.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from orchestrator.orchestrator import Orchestrator
from persistent_agents.dispatch_context import bind_dispatch
from persistent_agents.tests.test_dispatch_integration import context
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.test_chain_hop import _parent, _register_parent, _run_hop
from tests.test_chain_hop import orch as shared_hop_host

hop_host = shared_hop_host


class StartupObserved(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_actual_startup_only_starts_explicitly_enabled_assignment_runner(monkeypatch, enabled):
    from orchestrator import boot_posture
    from persistent_agents import runtime
    monkeypatch.setattr(boot_posture, "assert_production_posture", Mock())
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "persistent_agents" and enabled)
    created_service = SimpleNamespace()
    created_runner = SimpleNamespace(start=Mock())
    factory = Mock(return_value=created_service)
    runner_factory = Mock(return_value=created_runner)
    monkeypatch.setattr(runtime, "AssignmentService", factory)
    monkeypatch.setattr(runtime, "AssignmentRunner", runner_factory)

    def background(coroutine, *, name):
        coroutine.close()

    hub = SimpleNamespace(runtime_composition=SimpleNamespace(start=Mock()),
        generated_agent_publication_service=SimpleNamespace(
            recover_once=AsyncMock(return_value=SimpleNamespace(degraded_publication_ids=())), start=Mock()),
        _track_startup_background_task=background, _jwks_warm_loop=AsyncMock(),
        build_http_app=Mock(side_effect=StartupObserved),
        _work_recovery_loop=AsyncMock(),
        _personal_agent_watchdog_task=SimpleNamespace(done=lambda: False),
        _start_phi_warm=Mock(), _monitor_agents=AsyncMock(), web_sessions=object(),
        explicit_notes=SimpleNamespace(expiry_loop=AsyncMock()),
        lifecycle_manager=SimpleNamespace(reconcile_orphaned_draft_permissions=Mock(return_value=0),
            reconcile_legacy_directory_ownership=Mock()))
    with pytest.raises(StartupObserved):
        await Orchestrator._run_started_server(hub)
    assert hub._scheduler_loop is None
    if enabled:
        factory.assert_called_once_with(hub)
        assert runner_factory.call_args.args == (hub, created_service)
        assert runner_factory.call_args.kwargs == {}
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
async def test_dispatch_cannot_retry_or_publish_uncheckpointed_ui():
    hub = Orchestrator.__new__(Orchestrator)
    hub.MAX_RETRIES = 5
    hub.execute_tool_and_wait = AsyncMock(return_value=MCPResponse(error={"retryable": True}))
    hub._protected_dispatch_channel = lambda *a, **kw: "persistent_assignment"
    hub.rote = SimpleNamespace(adapt=Mock(), get_profile=Mock())
    hub._safe_send = AsyncMock()
    with bind_dispatch(context()):
        await hub._execute_with_retry(None, "reader", "read", {})
        assert await hub.send_ui_render(None, [{"type": "text", "text": "uncommitted"}]) is False
    hub.execute_tool_and_wait.assert_awaited_once()
    hub.rote.adapt.assert_not_called()
    hub.rote.get_profile.assert_not_called()
    hub._safe_send.assert_not_awaited()


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
async def test_chat_routes_exact_owner_command_to_control_service(monkeypatch):
    from persistent_agents import chat_tools
    handler = AsyncMock(return_value=MCPResponse(result={"assignments": []}))
    monkeypatch.setattr(chat_tools, "handle_meta_tool", handler)
    hub = Orchestrator.__new__(Orchestrator)
    hub._map_file_paths = lambda chat, args, **kw: args
    tool = SimpleNamespace(function=SimpleNamespace(name="ongoing_agent", arguments='{"command":"list"}'))
    mapping = {"ongoing_agent": chat_tools.META_AGENT_ID}
    socket = object()
    hub.ui_sessions = {socket: {"sub": "owner"}}
    result = await hub.execute_single_tool(socket, tool, mapping, chat_id="chat", user_id="owner")
    assert result.result == {"assignments": []}
    handler.assert_awaited_once_with(hub, "ongoing_agent", {"command": "list"},
                                    user_id="owner", chat_id="chat", websocket=socket)
