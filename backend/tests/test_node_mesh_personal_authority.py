"""Checks personal mesh authority from a live admission and explicit fake verified-principal records, without touching IAM.
Real signed enrollment and request goldens run in test_node_mesh_personal_authority_integration.py."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from orchestrator import node_mesh_admission as admission, vessel_executor
from orchestrator.hitl_confirmation import _arguments
from orchestrator.node_mesh import NodeMesh, NodeMeshError
from orchestrator.node_mesh_calls import CatalogTool, ResolvedTool, skill_definition
from shared import node_identity
from tests import fake_astral_kernel as fake
from tests.test_machine_scope_enforcement import _mesh_orchestrator

ISSUER = "https://auth.example.test/realms/astral"
OWNER = node_identity.canonical_owner_id(ISSUER, "user-1")


@pytest.fixture
def personal(monkeypatch):
    identity = node_identity.NodeIdentity.from_private(Ed25519PrivateKey.generate())
    monkeypatch.setattr(node_identity, "is_personal", lambda: True)
    monkeypatch.setattr(node_identity, "bound_owner", lambda: OWNER)
    monkeypatch.setattr(node_identity, "load_node_identity", lambda **_kwargs: identity)
    orch = _mesh_orchestrator()
    orch.derive_machine_authority = AsyncMock(side_effect=AssertionError("IAM must not be consulted"))
    orch.delegation.agent_service_client_id = "test-agent-service"
    skill = orch.agent_cards["agent"].skills[0]
    definition = skill_definition(skill)
    resolved = ResolvedTool("agent", "read", "tools:read", definition)
    arguments = _arguments({"q": "x"})
    action = _arguments({"tool": "read", "args_digest": "sha256:" + hashlib.sha256(arguments.encode()).hexdigest()})
    ticket = vessel_executor.CallTicket("{}", action, arguments)
    request = SimpleNamespace(
        id="request-1", chat_id="mesh-chat", from_device="phone", to_device="node", text="Read this.",
        call=None, state=SimpleNamespace(name="CLAIMED"),
    )
    state = SimpleNamespace(
        owner=OWNER, stopped=False, own=True, source=True, enrolled=True, proof_calls=0, on_proof=None,
        principal=SimpleNamespace(mesh_id="mesh-1", identities=[SimpleNamespace(issuer=ISSUER, subject="user-1")]),
        request=request,
    )

    class Kernel:
        def mesh_principal(self):
            state.proof_calls += 1
            if state.on_proof:
                state.on_proof()
            if state.stopped:
                raise fake.KernelError.State("gate.mesh_stopped", "stopped")
            if not state.own:
                raise fake.KernelError.State("gate.device_not_on_roster", "removed")
            return state.principal

        async def mesh_status(self):
            return SimpleNamespace(state=SimpleNamespace(name="ENROLLED" if state.enrolled else "NOT_ENROLLED"),
                                   mesh_id="mesh-1", device_id="node")

        def roster(self):
            members = [SimpleNamespace(device_id="node", is_local=True)] if state.own else []
            if state.source:
                members.append(SimpleNamespace(device_id="phone", is_local=False))
            return SimpleNamespace(mesh_id="mesh-1", stopped=state.stopped, members=members)

        def request(self, request_id):
            assert request_id == "request-1"
            return state.request

    mesh = NodeMesh.__new__(NodeMesh)
    mesh._kernel = Kernel()
    mesh._binding = fake
    mesh._started = True
    mesh.verify_ticket = AsyncMock()
    catalog = AsyncMock(return_value={("agent", "read"): CatalogTool(definition, "outward_read")})
    mint = AsyncMock()

    @contextmanager
    def bound(kind="turn"):
        if kind == "turn":
            with admission.turn(mesh, lambda: state.owner, OWNER, "local-chat",
                                {("agent", "read", definition): "tools:read"}, catalog, mint, request=request) as context:
                yield context
        else:
            request.call = SimpleNamespace(call_id="call-1", tool="read", arguments_json=arguments, action_json=action, ticket_json="{}")
            with admission.binding(mesh, lambda: state.owner, OWNER, "local-chat", resolved, ticket, request=request):
                with vessel_executor.carrying(ticket):
                    yield admission.current_admission()

    return SimpleNamespace(**locals())


@pytest.mark.parametrize("kind", ["turn", "call"])
async def test_active_mesh_authority_mints_a_node_only_credential_without_iam(personal, kind):
    host = personal
    with host.bound(kind):
        authority = await admission.personal_authority(host.orch, OWNER, agent_id="agent" if kind == "call" else None)
    claims = host.identity.verify_token(authority.access_token, audience=host.identity.node_id)
    assert claims["aud"] == [host.identity.node_id]
    assert claims["sub"] == OWNER and claims["exp"] - claims["iat"] <= 300
    assert claims["mesh_authority"] == {
        "mesh": "mesh-1", "member": "node", "request": "request-1", "chat": "mesh-chat",
        "local_chat": "local-chat", "source": "phone", "tools": [["agent", "read", host.definition]],
    }
    assert authority.allowed_scopes == ["tools:read"]
    assert authority.allowed_tools == ((("agent", "read", host.definition),) if kind == "turn" else None)
    assert authority.consent_ref == "mesh:mesh-1/request:request-1"
    host.orch.derive_machine_authority.assert_not_awaited()
    host.orch.delegation.exchange_token_for_agent.assert_not_awaited()
    if kind == "call":
        host.mesh.verify_ticket.assert_awaited()
    other = node_identity.NodeIdentity.from_private(Ed25519PrivateKey.generate())
    with pytest.raises(node_identity.NodeTokenError):
        other.verify_token(authority.access_token)
    with pytest.raises(node_identity.NodeTokenError):
        host.identity.verify_token(authority.access_token, audience="test-agent-service")


@pytest.mark.parametrize("kind", ["turn", "call"])
@pytest.mark.parametrize("change", ["owner", "kernel", "stopped", "removed", "source", "enrolled", "principal", "mesh", "request", "target", "state"])
async def test_current_enrollment_and_request_changes_refuse_authority(personal, kind, change):
    host = personal
    with host.bound(kind):
        if change == "owner":
            host.state.owner = "another-owner"
        elif change == "kernel":
            host.mesh._kernel = object()
        elif change == "stopped":
            host.state.stopped = True
        elif change == "removed":
            host.state.own = False
        elif change == "source":
            host.state.source = False
        elif change == "enrolled":
            host.state.enrolled = False
        elif change == "principal":
            host.state.principal.identities = [SimpleNamespace(issuer=ISSUER, subject="another-owner")]
        elif change == "mesh":
            host.state.principal.mesh_id = "another-mesh"
        elif change in ("request", "target", "state"):
            host.state.request = SimpleNamespace(**vars(host.request))
            setattr(host.state.request, {"request": "chat_id", "target": "to_device", "state": "state"}[change],
                    SimpleNamespace(name="CANCELLED") if change == "state" else "other")
        with pytest.raises(NodeMeshError):
            await admission.personal_authority(host.orch, OWNER, agent_id="agent" if kind == "call" else None)
    host.orch.derive_machine_authority.assert_not_awaited()


async def test_stale_or_absent_admission_never_uses_profile_or_environment_owner_as_proof(personal):
    with pytest.raises(NodeMeshError):
        await admission.personal_authority(personal.orch, OWNER)
    with personal.bound() as context:
        pass
    token = admission._TURN.set(context)
    try:
        with pytest.raises(NodeMeshError):
            await admission.personal_authority(personal.orch, OWNER)
    finally:
        admission._TURN.reset(token)


@pytest.mark.parametrize("change", ["owner", "kernel", "stop"])
async def test_a_binding_lost_while_reading_proof_refuses_authority(personal, change):
    host = personal

    def mutate():
        if change == "owner":
            host.state.owner = "another-owner"
        elif change == "kernel":
            host.mesh._kernel = object()
        else:
            host.state.stopped = True

    host.state.on_proof = mutate
    with host.bound():
        with pytest.raises(NodeMeshError):
            await admission.personal_authority(host.orch, OWNER)


async def test_old_kernel_without_principal_proof_fails_closed(personal):
    personal.mesh._kernel.mesh_principal = None
    with personal.bound():
        with pytest.raises(NodeMeshError):
            await admission.personal_authority(personal.orch, OWNER)


async def test_node_signing_key_failure_is_not_replaced_with_iam(personal, monkeypatch):
    def missing(**_kwargs):
        raise node_identity.NodeKeyError("missing test key")

    monkeypatch.setattr(node_identity, "load_node_identity", missing)
    with personal.bound():
        with pytest.raises(NodeMeshError):
            await admission.personal_authority(personal.orch, OWNER)
    personal.orch.derive_machine_authority.assert_not_awaited()


async def test_personal_orchestrator_runner_uses_only_its_live_mesh_authority(personal):
    from orchestrator.node_mesh_chat import orchestrator_runner

    host = personal
    host.orch.handle_chat_message = AsyncMock()
    bound_authorities = []
    bind = host.orch._bind_machine_turn

    def capture(socket, authority):
        bound_authorities.append(authority)
        bind(socket, authority)

    host.orch._bind_machine_turn = capture
    with host.bound():
        await orchestrator_runner(host.orch)(OWNER, "local-chat", "Read this.")
    assert len(bound_authorities) == 1
    assert bound_authorities[0].allowed_tools == (("agent", "read", host.definition),)
    host.orch.derive_machine_authority.assert_not_awaited()
    assert not host.orch.ui_sessions


async def test_personal_peer_authorization_requires_the_carried_admission(personal):
    from orchestrator.node_mesh_calls import CallRefused, OrchestratorCalls

    host = personal
    calls = OrchestratorCalls(host.orch)
    with pytest.raises(CallRefused):
        await calls.authorize(OWNER, host.resolved)
    with host.bound("call"):
        authority = await calls.authorize(OWNER, host.resolved)
    assert authority.agent_id == "agent" and authority.allowed_scopes == ["tools:read"]
    host.orch.derive_machine_authority.assert_not_awaited()


async def test_runner_cannot_use_the_admission_for_a_different_local_chat(personal):
    from orchestrator.node_mesh_chat import orchestrator_runner

    host = personal
    host.orch.handle_chat_message = AsyncMock()
    with host.bound():
        with pytest.raises(NodeMeshError):
            await orchestrator_runner(host.orch)(OWNER, "another-chat", "Read this.")
    host.orch.handle_chat_message.assert_not_awaited()


async def test_peer_authorization_cannot_relabel_the_admitted_tool(personal):
    from orchestrator.node_mesh_calls import CallRefused, OrchestratorCalls

    with personal.bound("call"):
        other = ResolvedTool("agent", "peek", "tools:read", personal.definition)
        with pytest.raises(CallRefused):
            await OrchestratorCalls(personal.orch).authorize(OWNER, other)


async def test_institution_profile_cannot_invoke_personal_mesh_derivation(personal, monkeypatch):
    monkeypatch.setattr(node_identity, "is_personal", lambda: False)
    with personal.bound():
        with pytest.raises(NodeMeshError, match="personal_mesh_required"):
            await admission.personal_authority(personal.orch, OWNER)


async def test_a_live_lease_without_a_request_is_not_mesh_identity(personal):
    host = personal
    with admission.turn(host.mesh, lambda: OWNER, OWNER, "local-chat", {}, host.catalog, host.mint):
        with pytest.raises(NodeMeshError):
            await admission.personal_authority(host.orch, OWNER)


@pytest.mark.parametrize("identity", [None, [], [SimpleNamespace(issuer=ISSUER, subject=None)]])
async def test_missing_or_malformed_attested_identities_fail_closed(personal, identity):
    personal.state.principal.identities = identity
    with personal.bound():
        with pytest.raises(NodeMeshError):
            await admission.personal_authority(personal.orch, OWNER)


async def test_changed_global_owner_refuses_a_previously_matching_live_context(personal, monkeypatch):
    monkeypatch.setattr(node_identity, "bound_owner", lambda: "wrong")
    with personal.bound():
        with pytest.raises(NodeMeshError, match="owner_mismatch"):
            await admission.personal_authority(personal.orch, OWNER)


async def test_peer_authority_requires_its_exact_agent_and_carried_ticket(personal):
    with personal.bound("call"):
        with pytest.raises(NodeMeshError, match="call_mismatch"):
            await admission.personal_authority(personal.orch, OWNER, agent_id="other")
        with vessel_executor.carrying(None):
            with pytest.raises(NodeMeshError):
                await admission.personal_authority(personal.orch, OWNER, agent_id="agent")


@pytest.mark.parametrize("field,value", [("id", ""), ("text", None), ("from_device", 1), ("continues", ""), ("continues", 7)])
async def test_malformed_request_references_cannot_open_an_authority_lease(personal, field, value):
    setattr(personal.request, field, value)
    with pytest.raises(NodeMeshError, match="malformed"):
        with personal.bound():
            pass


@pytest.mark.parametrize("change", ["stopped", "own"])
async def test_inconsistent_roster_cannot_be_hidden_by_a_principal_record(personal, change):
    host = personal
    host.mesh._kernel.mesh_principal = lambda: host.state.principal
    setattr(host.state, change, change == "stopped")
    with host.bound():
        with pytest.raises(NodeMeshError):
            await admission.personal_authority(host.orch, OWNER)


async def test_a_carried_ticket_for_other_arguments_cannot_authorize_a_peer_request(personal):
    from dataclasses import replace

    with personal.bound("call") as context:
        changed = replace(personal.ticket, arguments_json='{"q":"another"}')
        token = admission._CURRENT.set(replace(context, ticket=changed))
        try:
            with vessel_executor.carrying(changed):
                with pytest.raises(NodeMeshError, match="call_mismatch"):
                    await admission.personal_authority(personal.orch, OWNER, agent_id="agent")
        finally:
            admission._CURRENT.reset(token)


async def test_peer_authorization_still_applies_current_tool_permissions(personal):
    from orchestrator.node_mesh_calls import CallRefused, OrchestratorCalls

    personal.orch.tool_permissions.is_tool_allowed.return_value = False
    with personal.bound("call"):
        with pytest.raises(CallRefused, match="permission_denied"):
            await OrchestratorCalls(personal.orch).authorize(OWNER, personal.resolved)


@pytest.mark.parametrize("denied", [False, True])
async def test_personal_peer_authorization_occurs_after_claim_inside_the_ticket_binding(personal, tmp_path, denied):
    from orchestrator.node_mesh_calls import CallRefused
    from tests.test_node_mesh_chat import FakeCalls, build, call_for, serve

    checked = []

    class Calls(FakeCalls):
        async def authorize(self, owner, resolved):
            context = admission.current_admission()
            checked.append(context)
            assert context is not None and context.request.id == "r1"
            assert vessel_executor.current_ticket() is context.ticket
            assert kernel.request("r1").state == fake.RequestState.CLAIMED
            if denied:
                raise CallRefused("node.owner_mismatch")
            return await super().authorize(owner, resolved)

    calls = Calls()
    mesh, kernel, history, bridge = await build(tmp_path, owner=OWNER, calls=calls)
    try:
        kernel.add_chat("c", [])
        kernel.add_request("r1", "c", call=call_for())
        await serve(bridge, kernel, "r1")
        assert len(checked) == 1 and kernel.claimed == ["r1"]
        assert bool(calls.ran) is not denied
        if denied:
            assert kernel.refused == [("r1", "node.owner_mismatch")]
    finally:
        await bridge.stop()
