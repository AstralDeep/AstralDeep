"""Focused final-dispatch coverage for Feature 074 T159-T165/T168."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import Message as A2AMessage, Role

from orchestrator.a2a_orchestrator_executor import OrchestratorA2AExecutor
from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
from orchestrator.governed_dispatch import (
    DispatchRuntime,
    GovernedDispatchError,
    GovernedFinalDispatch,
)
from orchestrator.lets_gateway import LETS_CALLER_CAPABILITY, LetsGatewayError
from orchestrator.lets_audit import LetsAuditObserver
from orchestrator.orchestrator import Orchestrator
from shared.a2a_bridge import a2a_message_to_mcp_request, make_data_part
from shared.a2a_executor import MCPAgentExecutor
from shared.base_agent import BaseA2AAgent
from shared.protocol import MCP_PROTOCOL_VERSION, MCPRequest, MCPResponse


class _Permit:
    def __init__(self, *, enforced: bool) -> None:
        self.enforced = enforced
        self.released = False

    def caller_capabilities(self):
        if not self.enforced:
            return {}
        return {LETS_CALLER_CAPABILITY: {"type": "test-permit"}}

    def release(self) -> None:
        self.released = True


class _Gateway:
    def __init__(self, mode: str, *, failure: LetsGatewayError | None = None) -> None:
        self.config = SimpleNamespace(
            mode=mode,
            governed_cohorts=("server_dynamic", "byo_user"),
            governed_agent_allowlist=(),
        )
        self.failure = failure
        self.calls = []
        self.permits: list[_Permit] = []

    async def authorize(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        permit = _Permit(enforced=self.config.mode == "enforce")
        self.permits.append(permit)
        return permit


class _Plane:
    def __init__(self) -> None:
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield object()


class _Repository:
    def __init__(self) -> None:
        self.calls = []
        self.binding = SimpleNamespace(lease_sequence=17)

    def get_active_binding(self, transaction, **kwargs):
        self.calls.append((transaction, kwargs))
        return self.binding


def _runtime(*, conformant: bool = True) -> DispatchRuntime:
    return DispatchRuntime(
        owner_id="owner-1",
        agent_id="agent-1",
        population="server_dynamic",
        runtime_id="runtime-1",
        runtime_generation=4,
        executor_audience="executor-1",
        executor_conformant=conformant,
        dispatch_posture=(
            "protected_executor" if conformant else "dispatch_mediated_only"
        ),
    )


def _active(mode: str, *, failure: LetsGatewayError | None = None):
    gateway = _Gateway(mode, failure=failure)
    plane = _Plane()
    repository = _Repository()

    async def resolve(agent_id, owner_id):
        assert agent_id == "agent-1"
        assert owner_id == "owner-1"
        return _runtime()

    adapter = GovernedFinalDispatch.active(
        gateway=gateway,
        plane=plane,
        authority_repository=repository,
        runtime_resolver=resolve,
    )
    return adapter, gateway, plane, repository


@pytest.mark.asyncio
async def test_off_mode_is_an_exact_no_authority_bypass() -> None:
    adapter = GovernedFinalDispatch.off()
    opaque = object()
    final_arguments = {"object": opaque}
    seen = []

    async def invoke(capabilities):
        seen.append(capabilities)
        return "unchanged-result"

    result = await adapter.execute(
        owner_id=None,
        agent_id="anything",
        tool_id="anything",
        scope="not-even-a-scope",
        channel="rest",
        audit_correlation_id="unused",
        final_arguments=final_arguments,
        invoke=invoke,
    )

    assert result == "unchanged-result"
    assert seen == [{}]
    assert final_arguments == {"object": opaque}


@pytest.mark.parametrize(
    "channel",
    [
        "rest",
        "websocket",
        "a2a",
        "mcp",
        "background",
        "scheduled",
        "chained",
        "stream",
    ],
)
@pytest.mark.asyncio
async def test_every_channel_builds_context_after_exact_binding_lookup(channel) -> None:
    adapter, gateway, plane, repository = _active("enforce")
    final_arguments = {
        "query": "safe",
        "_credentials": {"ciphertext": "opaque"},
    }
    observed = []

    async def invoke(capabilities):
        observed.append((capabilities, dict(final_arguments)))
        return "ok"

    assert await adapter.execute(
        owner_id="owner-1",
        agent_id="agent-1",
        tool_id="search",
        scope="tools:read",
        channel=channel,
        audit_correlation_id="audit-1",
        final_arguments=final_arguments,
        invoke=invoke,
    ) == "ok"

    assert plane.transactions == 1
    assert repository.calls[0][1] == {
        "owner_id": "owner-1",
        "agent_id": "agent-1",
        "runtime_id": "runtime-1",
        "runtime_generation": 4,
    }
    context = gateway.calls[0]["context"]
    observer = gateway.calls[0]["observer"]
    assert isinstance(observer, LetsAuditObserver)
    assert observer.actor_user_id == "owner-1"
    assert observer.auth_principal == "owner-1"
    assert observer.agent_id == "agent-1"
    assert observer.strict is True
    assert context.channel == channel
    assert context.expected_sequence == 17
    assert set(observed[0][0]) == {LETS_CALLER_CAPABILITY}
    assert LETS_CALLER_CAPABILITY not in observed[0][1]
    assert observed[0][1] == final_arguments
    assert gateway.permits[0].released is True


@pytest.mark.asyncio
async def test_audit_identity_is_bound_to_the_same_dispatch_owner() -> None:
    adapter, gateway, _plane, _repository = _active("enforce")

    assert await adapter.execute(
        owner_id="owner-1",
        actor_user_id="owner-1",
        auth_principal="agent:initiator",
        conversation_id="chat-1",
        agent_id="agent-1",
        tool_id="search",
        scope="tools:read",
        channel="chained",
        audit_correlation_id="audit-1",
        final_arguments={"query": "safe"},
        invoke=lambda _capabilities: "ok",
    ) == "ok"

    observer = gateway.calls[0]["observer"]
    assert observer.actor_user_id == "owner-1"
    assert observer.auth_principal == "agent:initiator"
    assert observer.conversation_id == "chat-1"

    with pytest.raises(GovernedDispatchError, match="audit_actor_owner_mismatch"):
        await adapter.execute(
            owner_id="owner-1",
            actor_user_id="owner-2",
            agent_id="agent-1",
            tool_id="search",
            scope="tools:read",
            channel="rest",
            audit_correlation_id="audit-2",
            final_arguments={"query": "safe"},
            invoke=lambda _capabilities: "must-not-run",
        )


@pytest.mark.asyncio
async def test_each_possible_physical_attempt_gets_new_operation_and_nonce() -> None:
    adapter, gateway, _plane, _repository = _active("enforce")

    async def invoke(_capabilities):
        return "ok"

    for _ in range(2):
        await adapter.execute(
            owner_id="owner-1",
            agent_id="agent-1",
            tool_id="search",
            scope="tools:read",
            channel="websocket",
            audit_correlation_id="audit-1",
            final_arguments={"query": "identical"},
            invoke=invoke,
        )

    first = gateway.calls[0]["context"]
    second = gateway.calls[1]["context"]
    assert first.operation_id != second.operation_id
    assert first.nonce != second.nonce
    assert first.wire_arguments_sha256 == second.wire_arguments_sha256


@pytest.mark.asyncio
async def test_transport_fallback_reenters_adapter_for_each_physical_send() -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.agents = {"agent-1": object()}
    orchestrator.local_agents = {}
    orchestrator.a2a_clients = {"agent-1": "https://agent.invalid"}
    orchestrator.agent_urls = {}
    orchestrator.agent_cards = {}
    orchestrator.tool_permissions = MagicMock()
    orchestrator.tool_permissions.get_tool_scope.return_value = "tools:write"
    orchestrator._execute_via_websocket = AsyncMock(
        return_value=MCPResponse(
            error={
                "code": "known_remote_failure",
                "message": "known failure",
                "retryable": True,
            }
        )
    )
    orchestrator._execute_via_a2a = AsyncMock(
        return_value=MCPResponse(result={"ok": True})
    )

    class _PhysicalAdapter:
        mode = "enforce"

        def __init__(self):
            self.calls = []

        async def execute(self, **kwargs):
            self.calls.append(kwargs)
            capability = {
                LETS_CALLER_CAPABILITY: {"physical_attempt": len(self.calls)}
            }
            return await kwargs["invoke"](capability)

    adapter = _PhysicalAdapter()
    orchestrator.governed_final_dispatch = adapter
    args = {
        "value": "exact",
        "_delegation_token": "transport-only-secret",
    }

    response = await orchestrator._dispatch_tool_call(
        "agent-1",
        "write",
        args,
        5.0,
        None,
        protected_owner_id="owner-1",
        protected_channel="rest",
        protected_audit_correlation_id="audit-1",
    )

    assert response.result == {"ok": True}
    assert len(adapter.calls) == 2
    assert adapter.calls[0]["final_arguments"] == args
    assert adapter.calls[1]["final_arguments"] == {"value": "exact"}
    websocket_capabilities = (
        orchestrator._execute_via_websocket.await_args.kwargs[
            "caller_capabilities"
        ]
    )
    a2a_capabilities = orchestrator._execute_via_a2a.await_args.kwargs[
        "caller_capabilities"
    ]
    assert websocket_capabilities[LETS_CALLER_CAPABILITY][
        "physical_attempt"
    ] == 1
    assert a2a_capabilities[LETS_CALLER_CAPABILITY]["physical_attempt"] == 2
    assert orchestrator._execute_via_a2a.await_args.kwargs[
        "wire_arguments"
    ] == {"value": "exact"}


@pytest.mark.asyncio
async def test_shadow_authorization_failure_never_blocks_existing_actuator() -> None:
    adapter, gateway, _plane, _repository = _active(
        "shadow",
        failure=LetsGatewayError("warden_unavailable", retryable=True),
    )
    seen = []

    async def invoke(capabilities):
        seen.append(capabilities)
        return "existing-result"

    assert await adapter.execute(
        owner_id="owner-1",
        agent_id="agent-1",
        tool_id="search",
        scope="tools:read",
        channel="rest",
        audit_correlation_id="audit-1",
        final_arguments={"query": "safe"},
        invoke=invoke,
    ) == "existing-result"
    assert len(gateway.calls) == 1
    assert seen == [{}]


@pytest.mark.asyncio
async def test_optional_agent_allowlist_narrows_without_false_enforcement() -> None:
    gateway = _Gateway("enforce")
    gateway.config.governed_agent_allowlist = ("different-agent",)
    plane = _Plane()
    repository = _Repository()
    seen = []

    async def resolve(_agent_id, _owner_id):
        return _runtime()

    adapter = GovernedFinalDispatch.active(
        gateway=gateway,
        plane=plane,
        authority_repository=repository,
        runtime_resolver=resolve,
    )
    assert await adapter.execute(
        owner_id="owner-1",
        agent_id="agent-1",
        tool_id="search",
        scope="tools:read",
        channel="rest",
        audit_correlation_id="audit-1",
        final_arguments={"query": "safe"},
        invoke=lambda capabilities: seen.append(capabilities) or "existing",
    ) == "existing"

    assert seen == [{}]
    assert plane.transactions == 0
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_enforce_requires_authenticated_owner_and_conforming_executor() -> None:
    gateway = _Gateway("enforce")
    plane = _Plane()
    repository = _Repository()
    invoked = False

    async def invoke(_capabilities):
        nonlocal invoked
        invoked = True
        return "unexpected"

    for runtime, owner_id, code in (
        (_runtime(), None, "dispatch_owner_unavailable"),
        (_runtime(conformant=False), "owner-1", "executor_not_conformant"),
    ):
        async def resolve(_agent_id, _owner_id, selected=runtime):
            return selected

        adapter = GovernedFinalDispatch.active(
            gateway=gateway,
            plane=plane,
            authority_repository=repository,
            runtime_resolver=resolve,
        )
        with pytest.raises(GovernedDispatchError, match=code):
            await adapter.execute(
                owner_id=owner_id,
                agent_id="agent-1",
                tool_id="search",
                scope="tools:read",
                channel="a2a",
                audit_correlation_id="audit-1",
                final_arguments={"query": "safe"},
                invoke=invoke,
            )

    assert invoked is False
    assert gateway.calls == []


def test_background_and_scheduled_sockets_select_exact_dispatch_channels() -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.ui_sessions = {}
    background = VirtualWebSocket(
        BackgroundTask(
            task_id="background-a",
            chat_id="chat-a",
            user_id="owner-a",
            kind="async_chat",
        )
    )
    scheduled = VirtualWebSocket(
        BackgroundTask(
            task_id="scheduled-a",
            chat_id="chat-a",
            user_id="owner-a",
            kind="scheduled",
        )
    )

    assert orchestrator._protected_dispatch_channel(background) == "background"
    assert orchestrator._protected_dispatch_channel(scheduled) == "scheduled"


@pytest.mark.asyncio
async def test_external_and_unadmitted_byo_runtimes_are_dispatch_mediated_only() -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._governed_dispatch_runtimes = {}
    orchestrator.agents = {}
    orchestrator.local_agents = {}
    orchestrator.lifecycle_manager = None

    external = await orchestrator._resolve_governed_dispatch_runtime(
        "remote-agent", "owner-a"
    )
    assert external.population == "external"
    assert external.executor_conformant is False
    assert external.dispatch_posture == "dispatch_mediated_only"

    orchestrator.agents["byo-agent"] = SimpleNamespace(
        is_fenced_user_agent_tunnel=True,
        owner_sub="owner-a",
        runtime_fence=SimpleNamespace(
            runtime_instance_id="runtime-a",
            lifecycle_generation=1,
        ),
    )
    byo = await orchestrator._resolve_governed_dispatch_runtime(
        "byo-agent", "owner-a"
    )
    assert byo.population == "byo_user"
    assert byo.executor_conformant is False
    assert byo.executor_audience is None
    assert byo.dispatch_posture == "dispatch_mediated_only"


@pytest.mark.asyncio
async def test_existing_astral_security_denial_precedes_final_adapter() -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.security_flags = {
        "agent-1": {"search": {"blocked": True, "reason": "review"}}
    }
    orchestrator.ui_sessions = {}
    orchestrator.governed_final_dispatch = MagicMock()

    response = await orchestrator.execute_authorized_tool(
        claims={"sub": "owner-1"},
        user_id="owner-1",
        agent_id="agent-1",
        tool_name="search",
        arguments={"query": "safe"},
        channel="rest",
    )

    assert response.error is not None
    assert "system-blocked" in response.error["message"]
    orchestrator.governed_final_dispatch.execute.assert_not_called()


@pytest.mark.asyncio
async def test_unavailable_mode_is_shadow_bypass_but_enforce_refusal() -> None:
    seen = []

    async def invoke(capabilities):
        seen.append(capabilities)
        return "ok"

    common = dict(
        owner_id="owner-1",
        agent_id="agent-1",
        tool_id="search",
        scope="tools:read",
        channel="rest",
        audit_correlation_id="audit-1",
        final_arguments={"query": "safe"},
        invoke=invoke,
    )
    assert await GovernedFinalDispatch.unavailable("shadow").execute(**common) == "ok"
    with pytest.raises(GovernedDispatchError, match="governed_dispatch_unavailable"):
        await GovernedFinalDispatch.unavailable("enforce").execute(**common)
    assert seen == [{}]


@pytest.mark.asyncio
async def test_poll_stream_reauthorizes_before_each_actuator(monkeypatch) -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    websocket = object()
    orchestrator._get_user_id = MagicMock(return_value="owner-1")
    orchestrator._authorize_and_prepare = AsyncMock(
        return_value=SimpleNamespace(args={"query": "prepared"})
    )
    result = SimpleNamespace(error=None, ui_components=[], result={})
    orchestrator._execute_with_retry_audited = AsyncMock(return_value=result)
    orchestrator._safe_send = AsyncMock()
    orchestrator._stream_tasks = {id(websocket): {}}
    orchestrator._stream_subs = {id(websocket): {}}
    monkeypatch.setattr(
        "orchestrator.orchestrator.asyncio.sleep",
        AsyncMock(side_effect=__import__("asyncio").CancelledError),
    )

    await orchestrator._stream_loop(
        websocket,
        "search",
        "agent-1",
        1.0,
        {"query": "raw"},
        chat_id="chat-1",
    )

    gate = orchestrator._authorize_and_prepare.await_args
    assert gate.args[3] == {"query": "raw"}
    assert gate.kwargs["auto_subscribe_stream"] is False
    dispatch = orchestrator._execute_with_retry_audited.await_args
    assert dispatch.args[3] == {"query": "prepared"}
    assert dispatch.kwargs["channel"] == "stream"


@pytest.mark.asyncio
async def test_push_stream_open_binds_stream_id_and_uses_typed_capability(
    monkeypatch,
) -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    websocket = object()
    agent = SimpleNamespace(handle_mcp_request=AsyncMock())
    orchestrator.agents = {}
    orchestrator.local_agents = {"agent-1": agent}
    orchestrator.agent_cards = {}
    orchestrator.ui_sessions = {websocket: {"sub": "owner-1"}}
    orchestrator.tool_permissions = MagicMock()
    orchestrator.tool_permissions.get_tool_scope.return_value = "tools:read"
    orchestrator._authorize_and_prepare = AsyncMock(
        return_value=SimpleNamespace(
            args={"query": "prepared"},
            cap_job_id=None,
        )
    )

    class _Adapter:
        mode = "enforce"

        def __init__(self):
            self.calls = []

        async def execute(self, **kwargs):
            self.calls.append(kwargs)
            return await kwargs["invoke"](
                {LETS_CALLER_CAPABILITY: {"type": "test-permit"}}
            )

    adapter = _Adapter()
    orchestrator.governed_final_dispatch = adapter

    class _Audit:
        correlation_id = "audit-stream-1"

        def __init__(self, **_kwargs):
            self.outputs = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def set_outputs_meta(self, outputs):
            self.outputs = outputs

    monkeypatch.setattr("audit.hooks.ToolDispatchAudit", _Audit)

    request_id = await orchestrator._dispatch_stream_request(
        "agent-1",
        "search",
        {"query": "raw"},
        "stream-abcdef123456",
        "owner-1",
        websocket,
        "chat-1",
    )
    await __import__("asyncio").sleep(0)

    assert request_id.startswith("stream_search_")
    gate = orchestrator._authorize_and_prepare.await_args
    assert gate.kwargs["auto_subscribe_stream"] is False
    protected = adapter.calls[0]
    assert protected["channel"] == "stream"
    assert protected["final_arguments"] == {
        "arguments": {"query": "prepared"},
        "_stream": True,
        "_stream_id": "stream-abcdef123456",
    }
    request = agent.handle_mcp_request.await_args.args[1]
    assert set(request.caller_capabilities) == {LETS_CALLER_CAPABILITY}
    assert LETS_CALLER_CAPABILITY not in request.params["arguments"]


@pytest.mark.asyncio
async def test_unmarked_inprocess_executor_claims_at_last_shared_boundary(
    monkeypatch,
) -> None:
    order = []

    class _Server:
        protected_executor_at_actuator = False
        tools = {}

        def process_request(self, request):
            order.append(("actuator", dict(request.params["arguments"])))
            return MCPResponse(request_id=request.request_id, result={"ok": True})

    agent = BaseA2AAgent.__new__(BaseA2AAgent)
    agent.agent_id = "agent-1"
    agent.card = SimpleNamespace(version="1.0.0")
    agent.mcp_server = _Server()
    agent._logger = MagicMock()
    agent._decrypt_credentials_if_needed = MagicMock()
    agent._stream_wrapper_tasks = set()

    def verify(_request, *, final_wire_arguments):
        order.append(("claim", dict(final_wire_arguments)))

    agent._verify_and_claim_protected_request = MagicMock(side_effect=verify)
    websocket = SimpleNamespace(send_text=AsyncMock())
    monkeypatch.setattr(
        "shared.base_agent.flags.is_enabled",
        lambda _name: False,
    )
    monkeypatch.setenv("LETS_MODE", "off")
    monkeypatch.delenv("ASTRAL_RUNTIME_COHORT", raising=False)
    request = MCPRequest(
        request_id="request-1",
        method="tools/call",
        params={"name": "write", "arguments": {"value": "exact"}},
        protocol_version=MCP_PROTOCOL_VERSION,
        caller_capabilities={
            LETS_CALLER_CAPABILITY: {"type": "test-permit"}
        },
        caller_info={"name": "test", "version": "1"},
    )

    await agent.handle_mcp_request(websocket, request)

    assert order[0] == ("claim", {"value": "exact"})
    assert order[1][0] == "actuator"
    assert order[1][1]["value"] == "exact"
    assert "_runtime" in order[1][1]
    assert LETS_CALLER_CAPABILITY not in order[1][1]

    # Exact flag-off traffic with no typed permit skips even the local LETS
    # verifier seam and preserves the pre-integration actuator path.
    order.clear()
    agent._verify_and_claim_protected_request.reset_mock()
    off_request = MCPRequest(
        request_id="request-off",
        method="tools/call",
        params={"name": "write", "arguments": {"value": "off"}},
        protocol_version=MCP_PROTOCOL_VERSION,
        caller_capabilities={},
        caller_info={"name": "test", "version": "1"},
    )
    await agent.handle_mcp_request(websocket, off_request)
    agent._verify_and_claim_protected_request.assert_not_called()
    assert order[0][0] == "actuator"
    assert order[0][1]["value"] == "off"


def test_base_agent_final_verifier_receives_host_owned_lease_and_lineage(
    monkeypatch,
) -> None:
    gateway = MagicMock()
    agent = BaseA2AAgent.__new__(BaseA2AAgent)
    agent.agent_id = "agent-1"
    agent._lets_executor_runtime = SimpleNamespace(gateway=gateway)
    agent._lets_executor_initialization_error = None
    for name, value in {
        "LETS_MODE": "enforce",
        "ASTRAL_RUNTIME_COHORT": "server_dynamic",
        "ASTRAL_AUTHORITY_OWNER_ID": "owner-1",
        "ASTRAL_AUTHORITY_BINDING_ID": "binding-1",
        "ASTRAL_AUTHORITY_LEASE_ID": "lease-1",
        "ASTRAL_AUTHORITY_LINEAGE_ID": "lineage-1",
        "ASTRAL_RUNTIME_ID": "runtime-1",
        "ASTRAL_RUNTIME_GENERATION": "4",
        "LETS_EXECUTOR_INSTANCE_ID": "executor-1",
    }.items():
        monkeypatch.setenv(name, value)
    request = MCPRequest(
        request_id="request-1",
        method="tools/call",
        params={"name": "write", "arguments": {"value": "exact"}},
        protocol_version=MCP_PROTOCOL_VERSION,
        caller_capabilities={LETS_CALLER_CAPABILITY: {"permit": "outside"}},
        caller_info={"name": "test", "version": "1"},
    )

    agent._verify_and_claim_protected_request(
        request,
        final_wire_arguments={"value": "exact"},
    )

    assert gateway.verify_and_claim.call_args.kwargs["lease_id"] == "lease-1"
    assert gateway.verify_and_claim.call_args.kwargs["lineage_id"] == "lineage-1"


@pytest.mark.parametrize(
    "missing",
    ["ASTRAL_AUTHORITY_LEASE_ID", "ASTRAL_AUTHORITY_LINEAGE_ID"],
)
def test_base_agent_missing_lease_or_lineage_fails_before_final_verifier(
    monkeypatch,
    missing: str,
) -> None:
    gateway = MagicMock()
    agent = BaseA2AAgent.__new__(BaseA2AAgent)
    agent.agent_id = "agent-1"
    agent._lets_executor_runtime = SimpleNamespace(gateway=gateway)
    agent._lets_executor_initialization_error = None
    for name, value in {
        "LETS_MODE": "enforce",
        "ASTRAL_RUNTIME_COHORT": "server_dynamic",
        "ASTRAL_AUTHORITY_OWNER_ID": "owner-1",
        "ASTRAL_AUTHORITY_BINDING_ID": "binding-1",
        "ASTRAL_AUTHORITY_LEASE_ID": "lease-1",
        "ASTRAL_AUTHORITY_LINEAGE_ID": "lineage-1",
        "ASTRAL_RUNTIME_ID": "runtime-1",
        "ASTRAL_RUNTIME_GENERATION": "4",
        "LETS_EXECUTOR_INSTANCE_ID": "executor-1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)
    request = MCPRequest(
        request_id="request-1",
        method="tools/call",
        params={"name": "write", "arguments": {"value": "exact"}},
        protocol_version=MCP_PROTOCOL_VERSION,
        caller_capabilities={LETS_CALLER_CAPABILITY: {"permit": "outside"}},
        caller_info={"name": "test", "version": "1"},
    )

    with pytest.raises(LetsGatewayError, match="^executor_host_context_unavailable$"):
        agent._verify_and_claim_protected_request(
            request,
            final_wire_arguments={"value": "exact"},
        )
    gateway.verify_and_claim.assert_not_called()


def test_a2a_bridge_keeps_protected_metadata_outside_tool_arguments() -> None:
    message = A2AMessage(
        message_id="message-1",
        role=Role.ROLE_USER,
        parts=[
            make_data_part(
                {
                    "method": "tools/call",
                    "name": "search",
                    "arguments": {"query": "safe"},
                    "protocol_version": "2025-06-18",
                    "caller_capabilities": {
                        LETS_CALLER_CAPABILITY: {"type": "test-permit"}
                    },
                }
            )
        ],
    )

    request = a2a_message_to_mcp_request(message)

    assert request is not None
    assert request.params["arguments"] == {"query": "safe"}
    assert request.caller_capabilities == {
        LETS_CALLER_CAPABILITY: {"type": "test-permit"}
    }
    assert LETS_CALLER_CAPABILITY not in request.params["arguments"]


@pytest.mark.asyncio
async def test_a2a_executors_validate_actual_http_bearer_context() -> None:
    context = SimpleNamespace(
        call_context=SimpleNamespace(
            state={"headers": {"Authorization": "Bearer subject-token"}}
        )
    )
    claims = {"sub": "owner-1", "scope": "tools:read"}

    orchestrator_executor = OrchestratorA2AExecutor(MagicMock())
    orchestrator_executor.security_validator.validate_token = AsyncMock(
        return_value=claims
    )
    assert await orchestrator_executor._validated_invocation_identity(context) == (
        claims,
        "subject-token",
    )

    agent_executor = MCPAgentExecutor(MagicMock())
    agent_executor.security_validator.validate_token = AsyncMock(return_value=claims)
    assert await agent_executor._validated_bearer_claims(context) == claims
