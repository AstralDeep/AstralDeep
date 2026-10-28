"""Checks the fixed qualification tool's owner-bound admission, bounded state and dispatch identity.
Synthetic authorities cover local seams; live ordinary issuance remains separately qualified.
"""

import asyncio
from concurrent.futures import Future
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator import sandbox_tool_sessions as route
from shared.feature_flags import FeatureFlags


def test_tool_is_default_off_and_exposes_only_fixed_modes(monkeypatch):
    monkeypatch.delenv("FF_SANDBOX_QUALIFICATION", raising=False)
    assert FeatureFlags().is_enabled("sandbox_qualification") is False
    from agents.sandbox_qualification.mcp_server import TOOL_REGISTRY
    from orchestrator.local_agents import (
        FIRST_PARTY_PUBLIC_AGENT_IDS,
        BUILT_IN_AGENT_DIRS,
    )

    assert route.AGENT_ID in FIRST_PARTY_PUBLIC_AGENT_IDS
    assert "sandbox_qualification" not in BUILT_IN_AGENT_DIRS
    tool = TOOL_REGISTRY[route.TOOL_ID]
    assert tool["scope"] == "tools:write"
    schema = tool["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"mode", "qualification_id"}
    assert set(schema["properties"]["mode"]["enum"]) == set(route.PUBLIC_MODES)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"mode": "shell"},
        {"mode": "prepare", "command": "id"},
        {"mode": "authorized"},
        {"mode": "prepare", "qualification_id": "a" * 32},
    ],
)
def test_public_arguments_never_select_authority_or_code(arguments):
    with pytest.raises(route.ToolDenied):
        route.validate_arguments(arguments)


def test_direct_transport_has_no_trusted_actuator_context():
    from agents.sandbox_qualification.mcp_server import MCPServer
    from shared.protocol import MCPRequest

    server = MCPServer(SimpleNamespace())
    result = server.process_request(
        MCPRequest(
            request_id="direct",
            method="tools/call",
            params={
                "name": route.TOOL_ID,
                "arguments": {
                    "mode": "prepare",
                    "_sandbox_qualification_context": object(),
                },
            },
        )
    )
    assert result.error["code"] == "sandbox_qualification_denied"


@pytest.fixture
def manager(monkeypatch, tmp_path):
    config = SimpleNamespace(
        mode="enforce",
        executor_instance_id="host-executor",
        warden_origin="http://warden",
    )
    lifecycle = SimpleNamespace(
        admit_new_runtime=AsyncMock(),
        latest_binding=Mock(return_value=None),
        close_runtime_generation=AsyncMock(),
    )
    host = SimpleNamespace(
        lets_runtime=SimpleNamespace(ready=True, config=config, lifecycle=lifecycle)
    )
    service = route.QualificationToolSessions(host)
    monkeypatch.setattr(service, "_private_root", lambda: tmp_path)

    def executor(state, current):
        lifecycle.latest_binding.return_value = current
        return SimpleNamespace(close=Mock())

    monkeypatch.setattr(service, "_executor_for", executor)
    monkeypatch.setattr(service, "_prepare_runtime", lambda state: None)
    monkeypatch.setattr(service, "_verify_runtime", lambda state: None)
    monkeypatch.setattr(service, "_implementation_current", lambda: True)
    return service, lifecycle


def binding(owner="owner", runtime_id="runtime"):
    return SimpleNamespace(
        owner_id=owner,
        agent_id=route.AGENT_ID,
        runtime_id=runtime_id,
        runtime_generation=1,
        binding_id="binding",
        lease_id="lease",
        lineage_id="lineage",
        population=SimpleNamespace(value="server_dynamic"),
        state=SimpleNamespace(value="active", terminal=False),
        lease_expires_at_ns=time.time_ns() + 600_000_000_000,
    )


def context(owner="owner"):
    return SimpleNamespace(
        owner=owner, chat="chat", session="sid", source="source", current=lambda: True
    )


@pytest.mark.asyncio
async def test_prepare_needs_real_admission_and_rolls_back_before_ready(manager):
    service, lifecycle = manager
    lifecycle.admit_new_runtime.return_value = SimpleNamespace(
        protected=False, binding=None, error_code="denied"
    )
    with pytest.raises(route.ToolDenied):
        await service.prepare(context())
    assert not service._states
    lifecycle.admit_new_runtime.assert_awaited_once()
    assert lifecycle.admit_new_runtime.call_args.kwargs["declared_scopes"] == (
        "tools:write",
    )


@pytest.mark.asyncio
async def test_prepare_bounds_concurrent_owner_and_global_admissions(manager):
    service, lifecycle = manager
    arrived, release = asyncio.Event(), asyncio.Event()

    async def admit(**kwargs):
        arrived.set()
        await release.wait()
        return SimpleNamespace(
            protected=True,
            binding=binding(kwargs["owner_id"], kwargs["runtime_id"]),
            error_code=None,
        )

    lifecycle.admit_new_runtime.side_effect = admit
    first = asyncio.create_task(service.prepare(context()))
    await arrived.wait()
    with pytest.raises(route.ToolDenied):
        await service.prepare(context())
    service.MAX_ACTIVE = 1
    with pytest.raises(route.ToolDenied):
        await service.prepare(context("other"))
    release.set()
    result = await first
    assert result["status"] == "ready" and len(result["qualification_id"]) == 32
    assert len(service._states) == 1
    await service.shutdown()


@pytest.mark.asyncio
async def test_unknown_cross_owner_expired_and_displaced_binding_refuse(manager):
    service, lifecycle = manager

    async def admit(**kwargs):
        return SimpleNamespace(
            protected=True,
            binding=binding(kwargs["owner_id"], kwargs["runtime_id"]),
            error_code=None,
        )

    lifecycle.admit_new_runtime.side_effect = admit
    ready = await service.prepare(context())
    identity = ready["qualification_id"]
    state = service._states[identity]
    lifecycle.latest_binding.return_value = binding(runtime_id=state.runtime_id)
    assert (await service.runtime_for_owner("owner")).runtime_id == state.runtime_id
    with pytest.raises(route.ToolDenied):
        await service.resolve(context("other"), identity)
    with pytest.raises(route.ToolDenied):
        await service.resolve(context(), "f" * 32)
    lifecycle.latest_binding.return_value = binding(runtime_id="displaced")
    with pytest.raises(route.ToolDenied):
        await service.resolve(context(), identity)
    state.expires = 0
    with pytest.raises(route.ToolDenied):
        await service.resolve(context(), identity)
    await service.shutdown()


@pytest.fixture
def host(monkeypatch):
    from orchestrator import hitl_confirmation as hitl
    from orchestrator.orchestrator import Orchestrator
    from agents.sandbox_qualification.sandbox_qualification_agent import (
        SandboxQualificationAgent,
    )
    from shared.feature_flags import flags
    from shared.protocol import MCPRequest

    monkeypatch.setitem(flags._flags, "sandbox_qualification", True)
    monkeypatch.setitem(flags._flags, "inprocess_agents", True)
    monkeypatch.setattr(
        SandboxQualificationAgent,
        "_init_crypto",
        lambda self: setattr(self, "_public_key_jwk", {}),
    )
    orch = Orchestrator.__new__(Orchestrator)
    socket = object()
    claims = {"sub": "owner", "sid": "sid", "exp": time.time() + 600}
    orch.ui_sessions = {socket: claims}
    orch._ws_active_chat = {id(socket): "chat"}
    orch.cancelled_sessions = {}
    orch.agents, orch.a2a_clients = {}, {}
    orch._governed_dispatch_runtimes = {}
    orch.tool_permissions = SimpleNamespace(is_tool_allowed=Mock(return_value=True))
    agent = SandboxQualificationAgent(orch)
    orch.local_agents = {route.AGENT_ID: agent}
    orch.agent_cards = {route.AGENT_ID: agent.card}
    future = Future()
    request = MCPRequest(
        request_id="host",
        method="tools/call",
        params={"name": route.TOOL_ID, "arguments": {"mode": "prepare"}},
    )
    orch.pending_requests = {"host": future}
    orch._pending_request_agent = {"host": route.AGENT_ID}
    cancellation = hitl.cancellation_witness(orch, socket, "owner", "chat", create=True)
    call = hitl.PendingCall(
        "approval",
        "owner",
        "chat",
        route.AGENT_ID,
        route.TOOL_ID,
        hitl._arguments({"mode": "prepare"}),
        ("source_bound",),
        time.monotonic() + 600,
        websocket=socket,
        cancellation=cancellation,
    )
    approval = hitl.Approval(orch, call, consumed=True, effect_started=True)
    token = hitl._APPROVAL.set(approval)
    try:
        yield SimpleNamespace(
            orch=orch,
            socket=socket,
            agent=agent,
            request=request,
            claims=claims,
            approval=approval,
            future=future,
        )
    finally:
        hitl._APPROVAL.reset(token)


def test_exact_code_owned_actor_and_approved_host_context(host):
    assert route.trusted_service(host.orch) is host.agent.sessions
    route.bind_dispatch(host.orch, host.request, {"mode": "prepare"}, host.socket)
    ctx = host.request._sandbox_qualification_context
    assert ctx.current() and ctx.owner == "owner" and ctx.session == "sid"
    host.future.cancel()
    assert ctx.current() is False


@pytest.mark.parametrize(
    "loss",
    [
        "owner",
        "session",
        "expired",
        "permission",
        "source",
        "remote",
        "card",
        "actor",
        "server",
        "pending",
        "approval",
        "bypass",
        "grant",
        "machine",
    ],
)
def test_displaced_actor_or_current_authority_cannot_mark_actuator_trusted(
    host, monkeypatch, loss
):
    from dataclasses import replace

    route.bind_dispatch(host.orch, host.request, {"mode": "prepare"}, host.socket)
    ctx = host.request._sandbox_qualification_context
    if loss == "owner":
        host.claims["sub"] = "other"
    elif loss == "session":
        host.claims["sid"] = "other"
    elif loss == "expired":
        host.claims["exp"] = 0
    elif loss == "permission":
        host.orch.tool_permissions.is_tool_allowed.return_value = False
    elif loss == "source":
        host.agent.sessions._source_identity = ()
    elif loss == "remote":
        host.orch.agents[route.AGENT_ID] = object()
    elif loss == "card":
        host.agent.card.description = "displaced"
    elif loss == "actor":
        host.orch.local_agents[route.AGENT_ID] = object()
    elif loss == "server":
        host.agent.mcp_server.process_request = lambda request: None
    elif loss == "pending":
        host.orch.pending_requests.clear()
    elif loss == "approval":
        host.approval.consumed = False
    elif loss == "bypass":
        host.approval.call = replace(host.approval.call, approval_source="bypass")
    elif loss == "grant":
        host.approval.call = replace(host.approval.call, grant_id="task")
    elif loss == "machine":
        host.claims["_invocation_channel"] = "mcp"
    assert ctx.current() is False


@pytest.mark.asyncio
async def test_neutral_prepare_is_not_relabelled_and_dynamic_admission_is_owner_bound(
    host, monkeypatch
):
    method = host.orch._resolve_governed_dispatch_runtime
    runtime = await method(route.AGENT_ID, "owner")
    assert runtime.population == "builtin" and runtime.runtime_id is None
    assert runtime.executor_conformant is False
    host.agent.sessions.runtime_for_owner = AsyncMock(side_effect=route.ToolDenied())
    with pytest.raises(route.ToolDenied):
        await method(route.AGENT_ID, "owner")


@pytest.mark.asyncio
async def test_late_callback_and_untrusted_context_never_prepare(host):
    route.bind_dispatch(host.orch, host.request, {"mode": "prepare"}, host.socket)
    ctx = host.request._sandbox_qualification_context
    host.future.cancel()
    with pytest.raises(route.ToolDenied):
        await host.agent.sessions.execute(ctx, {"mode": "prepare"}, None)
    with pytest.raises(route.ToolDenied):
        await host.agent.sessions.execute(context(), {"mode": "prepare"}, None)


def test_host_injected_delegation_stays_in_permit_hash_and_outside_public_contract(
    host,
):
    wire = {"mode": "prepare", "_delegation_token": "opaque-server-token"}
    route.bind_dispatch(host.orch, host.request, wire, host.socket)
    ctx = host.request._sandbox_qualification_context
    assert ctx.arguments == {"mode": "prepare"}
    assert ctx.wire_arguments == wire and ctx.current()
    with pytest.raises(route.ToolDenied):
        route.validate_arguments(wire)
    for value in (
        None,
        {"mode": "prepare", "_delegation_token": 1},
        {"mode": "prepare", "_credentials": "x"},
    ):
        with pytest.raises(route.ToolDenied):
            route._wire_arguments(value)


@pytest.mark.asyncio
async def test_registered_local_route_reaches_prepare_only_with_live_host_binding(
    host, monkeypatch
):
    from shared.feature_flags import flags
    from shared.protocol import MCPResponse

    monkeypatch.setitem(flags._flags, "progress_streaming", False)
    host.orch.pending_ui_sockets = {}
    host.orch._dispatch_context = {}
    host.orch.agent_urls = {}
    host.orch.lets_runtime = SimpleNamespace(
        ready=True,
        config=SimpleNamespace(mode="enforce"),
        lifecycle=SimpleNamespace(
            admit_new_runtime=AsyncMock(
                return_value=SimpleNamespace(
                    protected=False, binding=None, error_code="denied"
                )
            ),
            latest_binding=Mock(return_value=None),
        ),
    )
    result = await host.orch._execute_in_process(
        route.AGENT_ID, route.TOOL_ID, {"mode": "prepare"}, ui_websocket=host.socket
    )
    assert result.error["code"] == "sandbox_qualification_admission_denied"
    admission = host.orch.lets_runtime.lifecycle.admit_new_runtime
    admission.assert_awaited_once()
    assert admission.call_args.kwargs["owner_id"] == "owner"
    assert not host.agent.sessions._states
    assert set(host.orch.pending_requests) == {"host"}
    assert not host.orch._dispatch_context
    assert isinstance(result, MCPResponse)


@pytest.mark.asyncio
async def test_server_list_and_failure_are_structured(host, monkeypatch):
    from shared.protocol import MCPRequest
    from shared.agent_runtime import AgentRuntime

    server = host.agent.mcp_server
    listing = server.process_request(MCPRequest(request_id="list", method="tools/list"))
    assert listing.result["tools"][0]["name"] == route.TOOL_ID
    route.bind_dispatch(host.orch, host.request, {"mode": "prepare"}, host.socket)
    host.request.params["arguments"]["_runtime"] = AgentRuntime(
        None, host.request, route.AGENT_ID, asyncio.get_running_loop()
    )
    host.request._protected_wire_arguments = {"mode": "prepare"}
    host.orch.lets_runtime = None
    result = await asyncio.to_thread(server.process_request, host.request)
    assert result.error["code"] == "sandbox_qualification_authority_unavailable"


@pytest.mark.asyncio
async def test_prepare_outage_keeps_capacity_reserved_until_authority_is_reconciled(
    manager,
):
    service, lifecycle = manager
    lifecycle.admit_new_runtime.side_effect = OSError("outage")
    lifecycle.latest_binding.side_effect = OSError("outage")
    with pytest.raises(OSError):
        await service.prepare(context())
    assert len(service._states) == 1
    state = next(iter(service._states.values()))
    assert state.closed and not state.settled and state.root is None
    with pytest.raises(route.ToolDenied):
        await service.prepare(context())
    await service.shutdown()


@pytest.mark.asyncio
async def test_executor_preparation_cancellation_waits_and_closes_late_helper(
    manager, monkeypatch
):
    import threading

    service, lifecycle = manager
    ready, release = threading.Event(), threading.Event()
    executor = SimpleNamespace(close=Mock())

    async def admit(**kwargs):
        return SimpleNamespace(
            protected=True,
            binding=binding(kwargs["owner_id"], kwargs["runtime_id"]),
            error_code=None,
        )

    lifecycle.admit_new_runtime.side_effect = admit

    def create(state, current):
        ready.set()
        release.wait(5)
        return executor

    monkeypatch.setattr(service, "_executor_for", create)
    task = asyncio.create_task(service.prepare(context()))
    await asyncio.to_thread(ready.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    executor.close.assert_called_once()
    assert all(
        state.root is None and state.closed for state in service._states.values()
    )


@pytest.mark.asyncio
async def test_total_state_is_bounded_and_failed_admission_does_not_create_ready_ids(
    manager,
):
    service, lifecycle = manager
    service.MAX_ACTIVE = 1
    service.MAX_TOTAL = 1
    lifecycle.admit_new_runtime.return_value = SimpleNamespace(
        protected=False, binding=None, error_code="denied"
    )
    lifecycle.latest_binding.return_value = None
    for _ in range(12):
        with pytest.raises(route.ToolDenied):
            await service.prepare(context())
        assert not service._states
    service._states["old"] = SimpleNamespace(settled=True)
    with pytest.raises(route.ToolDenied):
        await service.prepare(context())
    assert "old" not in service._states


@pytest.fixture
def local_effect(monkeypatch, tmp_path):
    from tests import test_sandbox_runtime_qualification as fixtures
    from orchestrator import sandbox_qualification as sq

    authority_root = tmp_path / "authority-fixture"
    authority_root.mkdir()
    values = fixtures.lets_values.__wrapped__(authority_root)
    executor = fixtures.lets_runtime.__wrapped__(values)
    permits = fixtures.receipt_factory.__wrapped__(values)
    artifact_root = tmp_path / "runtime-fixture"
    artifact_root.mkdir()
    runtime = fixtures.stub_runtime.__wrapped__(artifact_root)
    support = fixtures.available_support.__wrapped__()
    monkeypatch.setattr(sq, "probe_support", lambda **kwargs: support)
    monkeypatch.setattr(sq, "prepare_srt_runtime", lambda **kwargs: runtime)
    service = route.QualificationToolSessions(SimpleNamespace())
    monkeypatch.setattr(service, "_runtime_cache", lambda state: tmp_path / "cache")
    root = tmp_path / "private"
    root.mkdir()
    state = route.Session(
        __import__("uuid").uuid4().hex,
        "owner",
        "chat",
        "sid",
        "source",
        "runtime",
        time.monotonic() + 300,
        root=root,
        runtime=runtime,
        prepared_runtime=runtime,
        executor=executor,
        supervisor=fixtures.UnsandboxedEffectSupervisor(),
    )
    state.assets = service._asset_identity(runtime)
    state.binding = binding(runtime_id=state.runtime_id)
    service.orchestrator.lets_runtime = SimpleNamespace(
        ready=True,
        config=SimpleNamespace(mode="enforce"),
        lifecycle=SimpleNamespace(latest_binding=Mock(return_value=state.binding)),
    )
    try:
        yield SimpleNamespace(
            service=service, state=state, permits=permits, runtime=runtime
        )
    finally:
        service._terminate(state)
        executor.close()
        for path in sq.OUTSIDE_WRITE_ROOTS:
            candidate = (
                __import__("pathlib").Path(path) / f"astral-srt-{state.identity}.txt"
            )
            candidate.unlink(missing_ok=True)


def test_fixed_actuator_claims_standard_receipt_before_real_golden_scratch_effect(
    local_effect,
):
    effect = local_effect
    arguments = {
        "mode": route.sq.AUTHORIZED_MODE,
        "qualification_id": effect.state.identity,
    }
    permit = effect.permits(arguments)
    result = effect.service._effect(effect.state, context(), arguments, permit)
    assert result["status"] == route.sq.STATUS_PROBE
    assert not result["failure_codes"] and result["receipt_sha256"]
    assert "owner" not in result and "session_dir" not in result
    with pytest.raises(Exception):
        effect.service._effect(effect.state, context(), arguments, permit)
    assert all(
        s.process_tree_terminated and s.readers_joined and s.pipes_closed
        for s in effect.state.supervisor.snapshots()
    )


@pytest.mark.parametrize(
    "mode",
    ["read_secret", "write_outside", "network_connect", "unix_ipc", "symlink_escape"],
)
def test_real_unsandboxed_negative_controls_never_claim_containment(local_effect, mode):
    effect = local_effect
    arguments = {"mode": mode, "qualification_id": effect.state.identity}
    result = effect.service._effect(
        effect.state, context(), arguments, effect.permits(arguments)
    )
    assert result["status"] == route.sq.STATUS_FAILED
    assert result["failure_codes"]


@pytest.mark.parametrize("mode", ["exhaust_output", "hang"])
def test_fixed_actuator_bounds_actual_output_and_ready_hang(local_effect, mode):
    effect = local_effect
    arguments = {"mode": mode, "qualification_id": effect.state.identity}
    result = effect.service._effect(
        effect.state, context(), arguments, effect.permits(arguments)
    )
    assert result["status"] == route.sq.STATUS_PROBE and not result["failure_codes"], (
        result["failure_codes"]
    )


def test_namespace_profile_refuses_unsandboxed_child_and_cleans_descendants(
    local_effect,
):
    effect = local_effect
    arguments = {"mode": "spawn_descendant", "qualification_id": effect.state.identity}
    with pytest.raises(route.sq.SandboxQualificationError):
        effect.service._effect(
            effect.state, context(), arguments, effect.permits(arguments)
        )
    assert all(
        s.process_tree_terminated and s.readers_joined and s.pipes_closed
        for s in effect.state.supervisor.snapshots()
    )


def test_closed_or_late_context_never_claims_or_launches(local_effect):
    effect = local_effect
    arguments = {
        "mode": route.sq.AUTHORIZED_MODE,
        "qualification_id": effect.state.identity,
    }
    effect.state.closed = True
    effect.state.executor = SimpleNamespace(
        verify_and_claim=Mock(side_effect=AssertionError("no claim"))
    )
    with pytest.raises(route.ToolDenied):
        effect.service._effect(effect.state, context(), arguments, {})
    assert not effect.state.supervisor.snapshots()


def test_unavailable_host_never_consumes_permit(local_effect, monkeypatch):
    effect = local_effect
    sq = route.sq
    monkeypatch.setattr(
        sq,
        "probe_support",
        lambda **kwargs: sq.SandboxSupport(False, "linux", "x64", None, ()),
    )
    effect.state.executor = SimpleNamespace(
        verify_and_claim=Mock(side_effect=AssertionError("no claim"))
    )
    with pytest.raises(route.ToolDenied, match="host_unavailable"):
        effect.service._prepare_runtime(effect.state)
    assert not effect.state.supervisor.snapshots()


def effect_context(host, mode, identity):
    from dataclasses import replace
    from orchestrator import hitl_confirmation as hitl

    args = {"mode": mode, "qualification_id": identity}
    host.approval.call = replace(host.approval.call, arguments=hitl._arguments(args))
    host.request.params["arguments"] = args
    route.bind_dispatch(host.orch, host.request, args, host.socket)
    return host.request._sandbox_qualification_context, args


@pytest.fixture
def admitted_host(host, monkeypatch, tmp_path):
    monkeypatch.setattr(route.DispatchContext, "current", lambda self: True)
    service = host.agent.sessions
    state = route.Session(
        __import__("uuid").uuid4().hex,
        "owner",
        "chat",
        "sid",
        route._source(),
        str(__import__("uuid").uuid4()),
        time.monotonic() + 300,
        root=tmp_path / "private",
        executor=SimpleNamespace(close=Mock(), verify_and_claim=Mock()),
    )
    state.root.mkdir()
    state.binding = binding(runtime_id=state.runtime_id)
    terminal = binding(runtime_id=state.runtime_id)
    terminal.state = SimpleNamespace(value="closed", terminal=True)
    lifecycle = SimpleNamespace(
        latest_binding=Mock(return_value=state.binding),
        close_runtime_generation=AsyncMock(
            return_value=SimpleNamespace(binding=terminal)
        ),
    )
    host.orch.lets_runtime = SimpleNamespace(
        ready=True,
        config=SimpleNamespace(mode="enforce", executor_instance_id="host-executor"),
        lifecycle=lifecycle,
    )
    service._states[state.identity] = state
    return host, state, lifecycle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [route.sq.STATUS_PROBE, route.sq.STATUS_FAILED, route.sq.STATUS_UNAVAILABLE],
)
async def test_one_mode_attempt_is_serial_single_use_and_failure_closes_qualification(
    admitted_host, monkeypatch, outcome
):
    host, state, lifecycle = admitted_host
    service = host.agent.sessions
    ctx, args = effect_context(host, "authorized", state.identity)
    monkeypatch.setattr(
        service, "_effect", lambda state, context, args, metadata: {"status": outcome}
    )
    result = await service.execute(ctx, args, {"ordinary": "metadata"})
    assert result["status"] == outcome and state.worker is None and not state.busy
    if outcome == route.sq.STATUS_PROBE:
        assert not state.closed
        with pytest.raises(route.ToolDenied):
            await service.execute(ctx, args, {})
    else:
        assert state.closed and state.settled and state.root is None
    await service.shutdown()


@pytest.mark.asyncio
async def test_close_consumes_exact_ordinary_metadata_and_cleans_private_state(
    admitted_host,
):
    host, state, lifecycle = admitted_host
    ctx, args = effect_context(host, "close", state.identity)
    executor = state.executor
    metadata = {"ordinary": "single-use-permit"}
    result = await host.agent.sessions.execute(ctx, args, metadata)
    executor.verify_and_claim.assert_called_once_with(
        metadata=metadata,
        final_arguments=args,
        tool_id=route.TOOL_ID,
        tool_scope="tools:write",
    )
    assert result["status"] == "closed" and state.closed and state.root is None
    executor.close.assert_called_once()


@pytest.mark.asyncio
async def test_profile_before_golden_and_rewritten_wire_are_denied(admitted_host):
    host, state, lifecycle = admitted_host
    ctx, args = effect_context(host, "network_connect", state.identity)
    with pytest.raises(route.ToolDenied):
        await host.agent.sessions.execute(ctx, args, {})
    with pytest.raises(route.ToolDenied):
        await host.agent.sessions.execute(
            ctx, {**args, "_delegation_token": "changed"}, {}
        )
    assert not state.supervisor.snapshots()
    await host.agent.sessions.shutdown()


@pytest.mark.asyncio
async def test_cancel_during_prelaunch_marks_closed_before_waiting_for_late_worker(
    admitted_host, monkeypatch
):
    import threading

    host, state, lifecycle = admitted_host
    service = host.agent.sessions
    ctx, args = effect_context(host, "authorized", state.identity)
    entered, release = threading.Event(), threading.Event()
    observed = []

    def late_worker(state, context, args, metadata):
        entered.set()
        release.wait(5)
        observed.append(state.closed)
        return {"status": route.sq.STATUS_FAILED}

    monkeypatch.setattr(service, "_effect", late_worker)
    task = asyncio.create_task(service.execute(ctx, args, {}))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    while not state.closed:
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed == [True] and state.root is None and state.worker is None


@pytest.mark.asyncio
async def test_binding_revocation_during_effect_closes_and_joins_worker(
    admitted_host, monkeypatch
):
    import threading

    host, state, lifecycle = admitted_host
    service = host.agent.sessions
    ctx, args = effect_context(host, "authorized", state.identity)
    entered = threading.Event()

    def worker(state, context, args, metadata):
        entered.set()
        while not state.closed:
            time.sleep(0.001)
        return {"status": route.sq.STATUS_FAILED}

    monkeypatch.setattr(service, "_effect", worker)
    task = asyncio.create_task(service.execute(ctx, args, {}))
    await asyncio.to_thread(entered.wait, 5)
    lifecycle.latest_binding.return_value = binding(runtime_id="revoked")
    with pytest.raises(route.ToolDenied):
        await task
    assert state.closed and state.root is None and state.worker is None


@pytest.mark.asyncio
async def test_expiry_reaps_private_artifacts_and_prevents_reuse(admitted_host):
    host, state, lifecycle = admitted_host
    state.expires = 0
    await host.agent.sessions._expire(state)
    assert state.closed and state.root is None
    ctx, args = effect_context(host, "authorized", state.identity)
    with pytest.raises(route.ToolDenied):
        await host.agent.sessions.execute(ctx, args, {})


@pytest.mark.parametrize("target", ["service", "class_server", "class_agent"])
def test_displaced_callable_cannot_select_actuator_verification(
    host, monkeypatch, target
):
    if target == "service":
        host.agent.sessions._effect = lambda *args: {"status": "passed"}
    elif target == "class_server":
        monkeypatch.setattr(
            type(host.agent.mcp_server), "process_request", lambda *args: None
        )
    else:
        monkeypatch.setattr(type(host.agent), "handle_mcp_request", AsyncMock())
    assert route.trusted_service(host.orch) is None
    with pytest.raises(route.ToolDenied):
        route.bind_dispatch(host.orch, host.request, {"mode": "prepare"}, host.socket)


def test_runtime_cache_copies_only_bounded_verified_tarballs_into_private_state(
    tmp_path, monkeypatch
):
    import hashlib
    from dataclasses import replace

    payload = b"independent public package fixture"
    digest = hashlib.sha512(payload).hexdigest()
    pin = replace(route.sq.default_artifact_pin(), sha512_hex=digest)
    source = tmp_path / f"astral-srt-qualification-{pin.version}"
    source.mkdir()
    name = f"sandbox-runtime-{pin.version}.tgz"
    (source / name).write_bytes(payload)
    root = tmp_path / "private"
    root.mkdir()
    state = SimpleNamespace(root=root)
    service = route.QualificationToolSessions(SimpleNamespace())
    monkeypatch.setattr(route.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(route.sq, "default_artifact_pin", lambda: pin)
    monkeypatch.setattr(
        route.sq, "_locked_installation", lambda pin: (b"", b"", [("url", digest)])
    )
    cache = service._runtime_cache(state)
    assert (cache / name).read_bytes() == payload
    assert (cache / name).stat().st_mode & 0o777 == 0o400
    assert service._runtime_cache(state) == cache
    __import__("shutil").rmtree(cache)
    (source / name).write_bytes(b"corrupt")
    with pytest.raises(route.ToolDenied):
        service._runtime_cache(state)
    __import__("shutil").rmtree(cache)
    (source / name).unlink()
    (source / name).symlink_to(root / "unrelated")
    with pytest.raises(OSError):
        service._runtime_cache(state)


def test_executor_is_derived_from_normal_config_and_public_binding(
    tmp_path, monkeypatch
):
    from tests import test_sandbox_runtime_qualification as fixtures

    values = fixtures.lets_values.__wrapped__(tmp_path)
    environment, manifest, policy, warden = values
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    config = SimpleNamespace(
        mode="enforce",
        executor_instance_id="base-executor",
        trust_manifest=SimpleNamespace(warden_id=warden.warden_id),
        executor_db_root=tmp_path / "replay",
        executor_authority_root=tmp_path / "authority",
    )
    service = route.QualificationToolSessions(
        SimpleNamespace(
            lets_runtime=SimpleNamespace(ready=True, config=config, lifecycle=object())
        )
    )
    runtime_id = str(__import__("uuid").uuid4())
    state = SimpleNamespace(owner="owner", runtime_id=runtime_id)
    executor = service._executor_for(state, binding(runtime_id=runtime_id))
    try:
        assert executor.requires_permit and executor.host.owner_id == "owner"
        assert executor.host.runtime_id == runtime_id
        assert executor.host.executor_audience == state.authority.executor_audience
        assert executor.verifier is not None
    finally:
        executor.close()


@pytest.mark.asyncio
async def test_runtime_and_prepare_refuse_stale_source_and_authority(manager):
    service, lifecycle = manager
    ctx = context()
    ctx.current = lambda: False
    with pytest.raises(route.ToolDenied):
        await service.prepare(ctx)
    service.orchestrator.lets_runtime.ready = False
    with pytest.raises(route.ToolDenied):
        await service.prepare(context())
    assert not service._states


@pytest.mark.asyncio
async def test_flag_on_registers_the_real_fixed_card_through_normal_registration(
    monkeypatch,
):
    from orchestrator import local_agents
    from shared import attachment_materializer, attachment_resolver
    from shared.feature_flags import flags
    from agents.sandbox_qualification.sandbox_qualification_agent import (
        SandboxQualificationAgent,
    )

    monkeypatch.setattr(
        flags,
        "is_enabled",
        lambda name: name in {"sandbox_qualification", "inprocess_agents"},
    )
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    monkeypatch.setattr(
        attachment_resolver, "register_plane_runtime", lambda *args: True
    )
    monkeypatch.setattr(
        attachment_materializer, "register_materialization_service", lambda *args: True
    )
    monkeypatch.setattr(
        SandboxQualificationAgent,
        "_init_crypto",
        lambda self: setattr(self, "_public_key_jwk", {}),
    )
    plane = SimpleNamespace(
        runtime=object(),
        repositories=object(),
        blobs=object(),
        attachment_materializer=object(),
    )
    host = SimpleNamespace(
        runtime_composition=SimpleNamespace(plane=plane),
        local_agents={},
        register_agent=AsyncMock(),
    )
    assert await local_agents.register_built_ins(host) == [route.AGENT_ID]
    message = host.register_agent.call_args.args[1]
    assert message.agent_card is host.local_agents[route.AGENT_ID].card
    assert [(skill.id, skill.scope) for skill in message.agent_card.skills] == [
        (route.TOOL_ID, "tools:write")
    ]
    assert message.agent_card.skills[0].input_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_shutdown_reaps_qualification_even_after_feature_flag_turns_off(
    admitted_host, monkeypatch
):
    from shared.feature_flags import flags

    host, state, lifecycle = admitted_host
    monkeypatch.setitem(flags._flags, "sandbox_qualification", False)
    host.orch.async_task_manager = SimpleNamespace(
        drain=AsyncMock(return_value=0), stop_retention_sweep=AsyncMock()
    )
    monkeypatch.setattr(
        "orchestrator.framework_runtime.stop_framework_runtime", lambda orch: None
    )
    await host.orch._close_started_services_once()
    assert state.closed and state.root is None and state.executor is None
    lifecycle.close_runtime_generation.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_outage_is_reported_as_pending_and_keeps_bounded_reservation(
    admitted_host,
):
    host, state, lifecycle = admitted_host
    lifecycle.close_runtime_generation.side_effect = OSError("authority unavailable")
    ctx, args = effect_context(host, "close", state.identity)
    result = await host.agent.sessions.execute(ctx, args, {})
    assert result["status"] == "closing" and state.closed and not state.settled
    assert state.root is None and state.executor is None
    assert state.identity in host.agent.sessions._states


def test_normal_permission_catalog_keeps_diagnostic_as_explicit_reviewed_write(host):
    from orchestrator.source_bound import registered_action

    scopes = {skill.id: skill.scope for skill in host.agent.card.skills}
    action = registered_action(scopes, route.AGENT_ID, route.TOOL_ID)
    assert action.needs_review and action.scope == "tools:write"
    with pytest.raises(route.ToolDenied):
        route.validate_arguments({"mode": "prepare", "qualification_id": None})


@pytest.mark.asyncio
async def test_preflight_unavailability_after_admission_never_returns_ready_id(
    manager, monkeypatch
):
    service, lifecycle = manager

    async def admit(**kwargs):
        return SimpleNamespace(
            protected=True,
            binding=binding(kwargs["owner_id"], kwargs["runtime_id"]),
            error_code=None,
        )

    lifecycle.admit_new_runtime.side_effect = admit
    monkeypatch.setattr(
        service,
        "_prepare_runtime",
        Mock(side_effect=route.ToolDenied("sandbox_qualification_host_unavailable")),
        raising=False,
    )
    with pytest.raises(route.ToolDenied, match="host_unavailable"):
        await service.prepare(context())
    assert all(
        state.closed and state.root is None for state in service._states.values()
    )


def test_preflight_freezes_actual_asset_bytes_and_no_preflight_runs_after_permit(
    local_effect, monkeypatch
):
    effect = local_effect
    effect.service._prepare_runtime(effect.state)
    assert effect.state.runtime is effect.state.prepared_runtime and effect.state.assets
    monkeypatch.setattr(
        route.sq,
        "prepare_srt_runtime",
        Mock(side_effect=AssertionError("no install after permit")),
    )
    monkeypatch.setattr(
        route.sq,
        "probe_support",
        Mock(side_effect=AssertionError("no preflight after permit")),
    )
    arguments = {"mode": "authorized", "qualification_id": effect.state.identity}
    result = effect.service._effect(
        effect.state, context(), arguments, effect.permits(arguments)
    )
    assert result["status"] == route.sq.STATUS_PROBE


def test_mutated_executable_is_refused_before_actuator_claim(local_effect):
    effect = local_effect
    executable = effect.state.runtime.apply_seccomp_path
    executable.write_bytes(b"changed executable")
    effect.state.executor = SimpleNamespace(verify_and_claim=Mock())
    with pytest.raises(route.ToolDenied, match="runtime_changed"):
        effect.service._effect(effect.state, context(), {"mode": "authorized"}, {})
    effect.state.executor.verify_and_claim.assert_not_called()
    assert not effect.state.supervisor.snapshots()


@pytest.mark.asyncio
async def test_mutated_runtime_is_refused_before_fresh_gateway_issuance(
    local_effect, monkeypatch
):
    effect = local_effect
    state, service = effect.state, effect.service
    monkeypatch.setattr(service, "_implementation_current", lambda: True)
    state.runtime_id = str(__import__("uuid").uuid4())
    state.binding = binding(runtime_id=state.runtime_id)
    service._states[state.identity] = state
    terminal = binding(runtime_id=state.runtime_id)
    terminal.state = SimpleNamespace(value="closed", terminal=True)
    service.orchestrator.lets_runtime = SimpleNamespace(
        ready=True,
        config=SimpleNamespace(mode="enforce", executor_instance_id="base-executor"),
        lifecycle=SimpleNamespace(
            latest_binding=Mock(return_value=state.binding),
            close_runtime_generation=AsyncMock(
                return_value=SimpleNamespace(binding=terminal)
            ),
        ),
    )
    assert (await service.runtime_for_owner("owner")).runtime_id == state.runtime_id
    state.runtime.apply_seccomp_path.write_bytes(b"mutated")
    with pytest.raises(route.ToolDenied, match="runtime_changed"):
        await service.runtime_for_owner("owner")
    assert state.closed and state.root is None


def test_displaced_prepared_descriptor_cannot_choose_another_executable(local_effect):
    from dataclasses import replace

    effect = local_effect
    effect.state.runtime = replace(
        effect.state.runtime, cli_js=effect.state.runtime.node_path
    )
    with pytest.raises(route.ToolDenied):
        effect.service._verify_runtime(effect.state)


def test_revoked_binding_is_rechecked_immediately_before_actuator_claim(local_effect):
    effect = local_effect
    effect.service.orchestrator.lets_runtime.lifecycle.latest_binding.return_value = (
        binding(runtime_id="revoked")
    )
    effect.state.executor = SimpleNamespace(verify_and_claim=Mock())
    with pytest.raises(route.ToolDenied, match="binding_changed"):
        effect.service._effect(effect.state, context(), {"mode": "authorized"}, {})
    effect.state.executor.verify_and_claim.assert_not_called()
    assert not effect.state.supervisor.snapshots()


@pytest.mark.parametrize("boundary", ["assets", "binding", "claim"])
def test_expiry_during_final_validation_never_launches(local_effect, monkeypatch, boundary):
    effect = local_effect
    state = effect.state
    verify = effect.service._verify_runtime
    latest = effect.service.orchestrator.lets_runtime.lifecycle.latest_binding
    claim = state.executor.verify_and_claim

    def verify_assets(current):
        verify(current)
        if boundary == "assets":
            current.expires = time.monotonic() - 1

    def current_binding(**kwargs):
        current = latest(**kwargs)
        if boundary == "binding":
            state.expires = time.monotonic() - 1
        return current

    def consume(**kwargs):
        claim(**kwargs)
        if boundary == "claim":
            state.expires = time.monotonic() - 1

    monkeypatch.setattr(effect.service, "_verify_runtime", verify_assets)
    monkeypatch.setattr(
        effect.service.orchestrator.lets_runtime.lifecycle, "latest_binding", current_binding
    )
    spy = Mock(side_effect=consume)
    monkeypatch.setattr(type(state.executor), "verify_and_claim", spy)
    arguments = {"mode": "authorized", "qualification_id": state.identity}
    with pytest.raises(route.ToolDenied):
        effect.service._effect(state, context(), arguments, effect.permits(arguments))
    assert spy.call_count == (1 if boundary == "claim" else 0)
    assert not state.supervisor.snapshots()


@pytest.mark.parametrize("loss", ["closed", "shutdown"])
def test_final_context_validation_cannot_hide_a_concurrent_close(
    local_effect, monkeypatch, loss
):
    effect = local_effect
    lifecycle = effect.service.orchestrator.lets_runtime.lifecycle
    latest = lifecycle.latest_binding
    validating_final_context = False

    def current_binding(**kwargs):
        nonlocal validating_final_context
        current = latest(**kwargs)
        validating_final_context = True
        return current

    def current_context():
        if validating_final_context:
            if loss == "closed":
                effect.state.closed = True
            else:
                effect.service._closing = True
        return True

    monkeypatch.setattr(lifecycle, "latest_binding", current_binding)
    claim = Mock(wraps=effect.state.executor.verify_and_claim)
    monkeypatch.setattr(type(effect.state.executor), "verify_and_claim", claim)
    ctx = context()
    ctx.current = current_context
    args = {"mode": "authorized", "qualification_id": effect.state.identity}
    with pytest.raises(route.ToolDenied):
        effect.service._effect(effect.state, ctx, args, effect.permits(args))
    claim.assert_not_called()
    assert not effect.state.supervisor.snapshots()


@pytest.mark.asyncio
async def test_resolution_rechecks_expiry_after_current_binding(admitted_host):
    host, state, lifecycle = admitted_host
    ctx, _ = effect_context(host, "authorized", state.identity)

    def expire(**kwargs):
        state.expires = time.monotonic() - 1
        return state.binding

    lifecycle.latest_binding.side_effect = expire
    with pytest.raises(route.ToolDenied):
        await host.agent.sessions.resolve(ctx, state.identity)
    await host.agent.sessions.shutdown()


@pytest.mark.asyncio
async def test_runtime_resolution_rechecks_source_after_asset_validation(manager, monkeypatch):
    service, lifecycle = manager

    async def admit(**kwargs):
        return SimpleNamespace(
            protected=True,
            binding=binding(kwargs["owner_id"], kwargs["runtime_id"]),
            error_code=None,
        )

    lifecycle.admit_new_runtime.side_effect = admit
    ready = await service.prepare(context())
    state = service._states[ready["qualification_id"]]
    source_current = Mock(return_value=True)
    monkeypatch.setattr(service, "_implementation_current", source_current)
    monkeypatch.setattr(
        service, "_verify_runtime", lambda current: setattr(source_current, "return_value", False)
    )
    with pytest.raises(route.ToolDenied):
        await service.runtime_for_owner("owner")
    assert state.closed and state.root is None
    await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["terminal", "active", "outage", "mismatch", "unknown"])
async def test_closed_reservation_recovers_only_after_exact_terminal_confirmation(
    manager, tmp_path, recovery
):
    service, lifecycle = manager

    async def admit(**kwargs):
        return SimpleNamespace(
            protected=True,
            binding=binding(kwargs["owner_id"], kwargs["runtime_id"]),
            error_code=None,
        )

    lifecycle.admit_new_runtime.side_effect = admit
    ready = await service.prepare(context())
    state = service._states[ready["qualification_id"]]
    roots = [tmp_path / "replay", tmp_path / "anchors"]
    config = service.orchestrator.lets_runtime.config
    config.executor_db_root, config.executor_authority_root = roots
    for root in roots:
        private = root / state.runtime_id
        private.mkdir(parents=True)
        (private / "retained").write_text("preserve until terminal")
    lifecycle.close_runtime_generation.side_effect = OSError("synthetic outage")
    await service._close(state)
    assert state.closed and not state.settled and state.expiry_task is None
    assert all((root / state.runtime_id / "retained").is_file() for root in roots)
    terminal = binding(runtime_id=state.runtime_id)
    terminal.state = SimpleNamespace(value="closed", terminal=True)
    if recovery != "outage":
        lifecycle.close_runtime_generation.side_effect = None
        lifecycle.close_runtime_generation.return_value = SimpleNamespace(binding=terminal)
    if recovery == "mismatch":
        terminal.runtime_id = "other-runtime"
    if recovery == "unknown":
        lifecycle.close_runtime_generation.return_value = SimpleNamespace(binding=None)
    lifecycle.latest_binding.return_value = state.binding if recovery == "active" else terminal
    try:
        if recovery in {"terminal", "active"}:
            result = await service.prepare(context())
            assert result["status"] == "ready" and state.settled
            assert all(not (root / state.runtime_id).exists() for root in roots)
        else:
            with pytest.raises(route.ToolDenied, match="capacity"):
                await service.prepare(context())
            assert not state.settled
            assert all((root / state.runtime_id / "retained").is_file() for root in roots)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_unknown_admission_with_no_record_keeps_its_reservation(manager):
    service, lifecycle = manager
    lifecycle.admit_new_runtime.side_effect = OSError("unknown outcome")
    lifecycle.latest_binding.return_value = None
    with pytest.raises(OSError):
        await service.prepare(context())
    assert len(service._states) == 1
    state = next(iter(service._states.values()))
    assert state.closed and not state.settled
    with pytest.raises(route.ToolDenied, match="capacity"):
        await service.prepare(context())
    discovered = binding(runtime_id=state.runtime_id)
    terminal = binding(runtime_id=state.runtime_id)
    terminal.state = SimpleNamespace(value="closed", terminal=True)
    lifecycle.latest_binding.return_value = discovered
    lifecycle.close_runtime_generation.return_value = SimpleNamespace(binding=terminal)
    lifecycle.admit_new_runtime.side_effect = None
    lifecycle.admit_new_runtime.return_value = SimpleNamespace(
        protected=False, binding=None, error_code="denied"
    )
    with pytest.raises(route.ToolDenied, match="admission_denied"):
        await service.prepare(context())
    assert state.settled
    await service.shutdown()


@pytest.mark.asyncio
async def test_concurrent_close_and_cancelled_waiter_join_one_cleanup(admitted_host):
    host, state, lifecycle = admitted_host
    arrived, release = asyncio.Event(), asyncio.Event()
    terminal = binding(runtime_id=state.runtime_id)
    terminal.state = SimpleNamespace(value="closed", terminal=True)

    async def close(**kwargs):
        arrived.set()
        await release.wait()
        return SimpleNamespace(binding=terminal)

    lifecycle.close_runtime_generation.side_effect = close
    service = host.agent.sessions
    first = asyncio.create_task(service._close(state))
    await arrived.wait()
    second = asyncio.create_task(service._close(state))
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert state.closed and state.settled and state.root is None
    assert lifecycle.close_runtime_generation.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["admission", "executor", "preflight"])
@pytest.mark.parametrize("cancel_shutdown", [False, True])
async def test_shutdown_fences_and_joins_late_preparation(
    manager, monkeypatch, stage, cancel_shutdown
):
    import threading

    service, lifecycle = manager
    arrived, release_async = asyncio.Event(), asyncio.Event()
    release_thread = threading.Event()
    loop = asyncio.get_running_loop()
    helper = SimpleNamespace(close=Mock())
    created = []

    async def admit(**kwargs):
        if stage == "admission":
            arrived.set()
            await release_async.wait()
        return SimpleNamespace(
            protected=True,
            binding=binding(kwargs["owner_id"], kwargs["runtime_id"]),
            error_code=None,
        )

    def executor(state, current):
        if stage == "executor":
            loop.call_soon_threadsafe(arrived.set)
            release_thread.wait()
        created.append(helper)
        lifecycle.latest_binding.return_value = current
        return helper

    def preflight(state):
        if stage == "preflight":
            loop.call_soon_threadsafe(arrived.set)
            release_thread.wait()

    lifecycle.admit_new_runtime.side_effect = admit
    lifecycle.latest_binding.return_value = None
    monkeypatch.setattr(service, "_executor_for", executor)
    monkeypatch.setattr(service, "_prepare_runtime", preflight)
    prepare = asyncio.create_task(service.prepare(context()))
    await arrived.wait()
    shutdown = asyncio.create_task(service.shutdown())
    await asyncio.sleep(0)
    if cancel_shutdown:
        shutdown.cancel()
    release_async.set()
    release_thread.set()
    try:
        if cancel_shutdown:
            with pytest.raises(asyncio.CancelledError):
                await shutdown
        else:
            await shutdown
        result = await asyncio.gather(prepare, return_exceptions=True)
        assert isinstance(result[0], (route.ToolDenied, asyncio.CancelledError))
        assert helper.close.call_count == len(created)
        assert all(
            state.closed and state.root is None and state.executor is None
            and state.expiry_task is None
            for state in service._states.values()
        )
        with pytest.raises(route.ToolDenied):
            await service.prepare(context("new-owner"))
    finally:
        release_async.set()
        release_thread.set()
        await asyncio.gather(prepare, shutdown, return_exceptions=True)
        await service.shutdown()
