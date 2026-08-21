"""Feature 074 T173: every Astral refusal remains ahead of LETS."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.governed_dispatch import (
    DispatchRuntime,
    GovernedDispatchError,
    GovernedFinalDispatch,
)
from orchestrator.orchestrator import GateRefusal, Orchestrator


@pytest.fixture
def gate_orchestrator(monkeypatch):
    from orchestrator import hitl, policy, supervisor, taint
    from shared.feature_flags import flags

    monkeypatch.setattr(policy, "policy_enabled", lambda: False)
    monkeypatch.setattr(taint, "taint_enabled", lambda: False)
    monkeypatch.setattr(supervisor, "supervisor_enabled", lambda: False)
    monkeypatch.setattr(hitl, "hitl_enabled", lambda: False)
    monkeypatch.setitem(flags._flags, "hook_system", False)

    websocket = object()
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.security_flags = {}
    orchestrator.ui_sessions = {websocket: {"sub": "owner-a"}}
    orchestrator.agent_cards = {}
    orchestrator.agents = {
        "agent-a": object(),
        "remote-compute-1": object(),
    }
    orchestrator.a2a_clients = {}
    orchestrator.local_agents = {}
    orchestrator.tool_permissions = MagicMock()
    orchestrator.tool_permissions.is_tool_allowed.return_value = True
    orchestrator.credential_manager = MagicMock()
    orchestrator.credential_manager.get_agent_credentials_encrypted.return_value = None
    orchestrator._map_file_paths = lambda _chat, values, **_kwargs: values
    orchestrator._tool_accepts_context_arg = lambda *_args: False
    orchestrator._policy_roles = lambda _websocket: []
    orchestrator._get_delegation_token = AsyncMock(return_value="delegation-token")
    orchestrator._delegation_required = lambda: False
    orchestrator._delegation_denied_for_permissions = AsyncMock(return_value=False)
    orchestrator._is_long_running_tool = lambda *_args: False
    orchestrator._auto_subscribe_stream_artifacts = AsyncMock()
    orchestrator.hooks = SimpleNamespace(emit=AsyncMock())
    orchestrator.governed_final_dispatch = SimpleNamespace(execute=AsyncMock())
    return orchestrator, websocket


async def _deny(orchestrator, websocket, *, agent_id="agent-a") -> GateRefusal:
    result = await orchestrator._authorize_and_prepare(
        websocket,
        agent_id,
        "clinical.search_v2",
        {"query": "safe"},
        "chat-a",
        "owner-a",
        auto_subscribe_stream=False,
    )
    assert isinstance(result, GateRefusal)
    orchestrator.governed_final_dispatch.execute.assert_not_awaited()
    return result


@pytest.mark.asyncio
async def test_security_denial_precedes_lets(gate_orchestrator) -> None:
    orchestrator, websocket = gate_orchestrator
    orchestrator.security_flags = {
        "agent-a": {"clinical.search_v2": {"blocked": True, "reason": "unsafe"}}
    }
    assert "system-blocked" in str((await _deny(orchestrator, websocket)).response.error)


@pytest.mark.asyncio
async def test_identity_denial_precedes_lets(gate_orchestrator, monkeypatch) -> None:
    from orchestrator import agent_identity

    orchestrator, websocket = gate_orchestrator
    orchestrator.agent_cards["agent-a"] = SimpleNamespace(name="Identity agent")
    monkeypatch.setattr(agent_identity, "identity_requirement_satisfied", lambda *_: False)
    monkeypatch.setattr(agent_identity, "identity_access_message", lambda *_: "identity required")
    assert "identity required" in str((await _deny(orchestrator, websocket)).response.error)


@pytest.mark.asyncio
async def test_permission_denial_precedes_lets(gate_orchestrator) -> None:
    orchestrator, websocket = gate_orchestrator
    orchestrator.tool_permissions.is_tool_allowed.return_value = False
    assert "restricted" in str((await _deny(orchestrator, websocket)).response.error)


@pytest.mark.asyncio
async def test_policy_denial_precedes_lets(gate_orchestrator, monkeypatch) -> None:
    from orchestrator import policy

    orchestrator, websocket = gate_orchestrator
    monkeypatch.setattr(policy, "policy_enabled", lambda: True)
    monkeypatch.setattr(policy, "load_rules", lambda: ())
    monkeypatch.setattr(
        policy,
        "evaluate_policy",
        lambda *_args: policy.PolicyDecision(
            effect=policy.DENY,
            reason="policy denied",
            rule_id="rule-a",
        ),
    )
    assert "policy denied" in str((await _deny(orchestrator, websocket)).response.error)


@pytest.mark.asyncio
async def test_taint_denial_precedes_lets(gate_orchestrator, monkeypatch) -> None:
    from orchestrator import taint

    orchestrator, websocket = gate_orchestrator
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    monkeypatch.setattr(taint, "is_sink", lambda *_args: True)
    monkeypatch.setattr(taint, "check_flow", lambda _trust: "deny")
    monkeypatch.setattr(taint, "trust_name", lambda _trust: "untrusted")
    orchestrator._taint_tracker = lambda _chat: SimpleNamespace(
        effective_trust_of_args=lambda _args: 0
    )
    assert "untrusted" in str((await _deny(orchestrator, websocket)).response.error)


@pytest.mark.asyncio
async def test_confirmation_denial_precedes_lets(
    gate_orchestrator,
    monkeypatch,
) -> None:
    from orchestrator import remote_confirmation

    orchestrator, websocket = gate_orchestrator
    monkeypatch.setattr(
        remote_confirmation,
        "evaluate",
        lambda *_args, **_kwargs: ("confirmation required", []),
    )
    result = await _deny(orchestrator, websocket, agent_id="remote-compute-1")
    assert "confirmation required" in str(result.response.error)


@pytest.mark.asyncio
async def test_egress_hitl_denial_precedes_lets(gate_orchestrator, monkeypatch) -> None:
    from orchestrator import hitl

    orchestrator, websocket = gate_orchestrator
    monkeypatch.setattr(hitl, "hitl_enabled", lambda: True)
    monkeypatch.setattr(hitl, "assess_risk", lambda *_args, **_kwargs: ("egress",))
    monkeypatch.setattr(hitl, "requires_confirmation", lambda _risks: True)
    monkeypatch.setattr(
        hitl,
        "confirmation_request",
        lambda *_args: SimpleNamespace(summary="egress confirmation required"),
    )
    assert "egress" in str((await _deny(orchestrator, websocket)).response.error)


@pytest.mark.asyncio
async def test_phi_hook_denial_precedes_lets(gate_orchestrator, monkeypatch) -> None:
    from shared.feature_flags import flags

    orchestrator, websocket = gate_orchestrator
    monkeypatch.setitem(flags._flags, "hook_system", True)
    orchestrator.hooks.emit.return_value = SimpleNamespace(
        action="block",
        reason="PHI policy denied",
        modified_args=None,
    )
    assert "PHI policy denied" in str((await _deny(orchestrator, websocket)).response.error)


@pytest.mark.asyncio
async def test_delegation_denial_precedes_lets(gate_orchestrator) -> None:
    orchestrator, websocket = gate_orchestrator
    orchestrator._get_delegation_token.return_value = None
    orchestrator._delegation_required = lambda: True
    assert "delegated authorization" in str(
        (await _deny(orchestrator, websocket)).response.error
    )


@pytest.mark.asyncio
async def test_owner_isolation_denial_precedes_lets_authorization() -> None:
    gateway = SimpleNamespace(
        config=SimpleNamespace(
            mode="enforce",
            governed_cohorts=("server_dynamic",),
            governed_agent_allowlist=(),
        ),
        authorize=AsyncMock(),
    )

    class Plane:
        @contextmanager
        def transaction(self):
            yield object()

    repository = SimpleNamespace(get_active_binding=MagicMock())

    async def resolve(_agent_id, _owner_id):
        return DispatchRuntime(
            owner_id="owner-b",
            agent_id="agent-a",
            population="server_dynamic",
            runtime_id="runtime-a",
            runtime_generation=1,
            executor_audience="executor-a",
            executor_conformant=True,
            dispatch_posture="protected_executor",
        )

    dispatch = GovernedFinalDispatch.active(
        gateway=gateway,
        plane=Plane(),
        authority_repository=repository,
        runtime_resolver=resolve,
    )
    with pytest.raises(GovernedDispatchError, match="runtime_owner_mismatch"):
        await dispatch.execute(
            owner_id="owner-a",
            agent_id="agent-a",
            tool_id="clinical.search_v2",
            scope="tools:read",
            channel="rest",
            audit_correlation_id="audit-a",
            final_arguments={"query": "safe"},
            invoke=lambda _capabilities: pytest.fail("owner-denied effect executed"),
        )
    gateway.authorize.assert_not_awaited()
    repository.get_active_binding.assert_not_called()


@pytest.mark.asyncio
async def test_astral_audit_start_failure_precedes_final_dispatch(
    gate_orchestrator,
    monkeypatch,
) -> None:
    orchestrator, websocket = gate_orchestrator
    orchestrator._execute_with_retry = AsyncMock()

    class RefusingAudit:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("audit unavailable")

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("audit.hooks.ToolDispatchAudit", RefusingAudit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await orchestrator._execute_with_retry_audited(
            websocket,
            "agent-a",
            "clinical.search_v2",
            {"query": "safe"},
            "chat-a",
            "owner-a",
            channel="rest",
        )
    orchestrator._execute_with_retry.assert_not_awaited()
    orchestrator.governed_final_dispatch.execute.assert_not_awaited()
