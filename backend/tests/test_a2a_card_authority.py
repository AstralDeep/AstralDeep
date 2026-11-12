"""Verifies that global A2A key pins change only through a current socket write by the bound node owner or the existing administrator policy."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import auth, human_request_authority as human_authority
from orchestrator.a2a_card_trust import CardTrust, CardTrustRegistry
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.orchestrator import Orchestrator
from persistent_agents.models import AssignmentError
from shared import node_identity
from tests.a2a_card_signing import TestSigner
from tests.helpers.database_clock import expire_host_clock
from tests.test_human_socket_authority_088 import (
    bound as bound, fixture as fixture, human as human, runtime as runtime,
    service as service, signing_key as signing_key, socket_request as socket_request,
)

BASE = "https://peer.example"
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def registration(human, socket_request, fixture, monkeypatch):
    monkeypatch.delenv(node_identity.PROFILE_ENV, raising=False)
    monkeypatch.delenv(node_identity.OWNER_ENV, raising=False)
    orch = human[2]
    socket, context, message = socket_request
    socket.scope["headers"] = [(k, v) for k, v in socket.scope["headers"] if k != b"cookie"]
    signer = TestSigner()
    message.update(action="register_external_agent", payload={"url": BASE, "public_key": signer.public_jwk})
    registry = CardTrustRegistry()
    original = CardTrust(public_key=TestSigner().public_jwk)
    registry.pin(BASE, original)
    orch._card_trust_registry = lambda: registry
    orch.send_ui_render = AsyncMock()
    orch._safe_send = AsyncMock()
    orch.discover_a2a_agent = AsyncMock()
    orch.send_agent_list = AsyncMock()
    orch.agent_cards = {}

    async def claims(**changes):
        token = fixture[3](**changes)
        verified = await auth.verify_user(await auth.verify_production_token(token))
        orch.ui_sessions[socket] = {**verified, "_raw_token": token}

    def capture():
        return human_authority.capture_human_socket_request(
            human[1], websocket=socket, context=context, message=message)

    return SimpleNamespace(orch=orch, socket=socket, context=context, message=message,
                           registry=registry, original=original, claims=claims,
                           capture=capture, owner=fixture[1], signer=signer)


@contextmanager
def request_context(pending):
    token = _CONNECTION_OPERATION_CONTEXT.set({"human_request": pending})
    try:
        yield
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
        if pending is not None:
            pending.close()


async def invoke(registration, pending):
    with request_context(pending):
        await Orchestrator._register_external_agent(
            registration.orch, registration.socket, registration.message["payload"])


async def test_global_registration_requires_a_captured_fresh_write(registration):
    with pytest.raises(AssignmentError):
        await invoke(registration, None)
    assert registration.registry.trust_for(BASE) is registration.original


async def test_a_regular_user_cannot_replace_another_owners_global_pin(registration):
    with pytest.raises(AssignmentError):
        await invoke(registration, registration.capture())
    assert registration.registry.trust_for(BASE) is registration.original
    registration.orch.discover_a2a_agent.assert_not_awaited()


@pytest.mark.parametrize("claims", [
    {"realm_access": {"roles": ["user", "admin"]}},
    {"resource_access": {"astral-web": {"roles": ["admin"]}}},
])
async def test_existing_admin_roles_can_register_through_a_real_write(registration, claims):
    await registration.claims(**claims)
    await invoke(registration, registration.capture())
    assert registration.registry.trust_for(BASE).public_key == registration.signer.public_jwk
    registration.orch.discover_a2a_agent.assert_awaited_once_with(BASE)


async def test_an_admin_role_from_an_unrelated_client_cannot_register(registration):
    await registration.claims(resource_access={"unrelated-client": {"roles": ["admin"]}})
    with pytest.raises(AssignmentError):
        await invoke(registration, registration.capture())
    assert registration.registry.trust_for(BASE) is registration.original


async def test_the_bound_personal_node_owner_can_register_without_admin(registration, monkeypatch):
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.OWNER_ENV, registration.owner)
    await invoke(registration, registration.capture())
    assert registration.registry.trust_for(BASE).public_key == registration.signer.public_jwk


async def test_a_different_personal_node_owner_is_refused_even_if_admin(registration, monkeypatch):
    await registration.claims(realm_access={"roles": ["user", "admin"]})
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.OWNER_ENV, "oidc.other.owner")
    with pytest.raises(AssignmentError):
        await invoke(registration, registration.capture())
    assert registration.registry.trust_for(BASE) is registration.original


async def test_an_expired_registered_socket_cannot_change_trust(registration, monkeypatch):
    await registration.claims(realm_access={"roles": ["user", "admin"]})
    pending = registration.capture()
    assert pending is not None
    expire_host_clock(monkeypatch, human_authority, after=pending.expiry)
    with pytest.raises(AssignmentError):
        await invoke(registration, pending)
    assert registration.registry.trust_for(BASE) is registration.original


async def test_mutated_request_and_delivery_loss_preserve_the_existing_pin(registration, monkeypatch):
    await registration.claims(realm_access={"roles": ["user", "admin"]})
    pending = registration.capture()
    registration.message["payload"] = {**registration.message["payload"], "owner_id": registration.owner}
    with pytest.raises(AssignmentError):
        await invoke(registration, pending)
    assert registration.registry.trust_for(BASE) is registration.original

    parser = CardTrust.from_mapping

    def lose_delivery(value):
        parsed = parser(value)
        registration.socket.closed = True
        return parsed

    monkeypatch.setattr(CardTrust, "from_mapping", lose_delivery)
    with pytest.raises(AssignmentError):
        await invoke(registration, registration.capture())
    assert registration.registry.trust_for(BASE) is registration.original


@pytest.mark.parametrize("change", ["different_owner", "unbound", "unreadable"])
async def test_owner_binding_loss_during_authentication_cannot_change_trust(registration, monkeypatch, change):
    await registration.claims(realm_access={"roles": ["user", "admin"]})
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.OWNER_ENV, registration.owner)
    pending = registration.capture()
    authenticate = pending.authenticate

    async def changed_binding():
        caller = await authenticate()
        if change == "unreadable":
            def unavailable():
                raise node_identity.NodeIdentityError("owner binding unreadable")
            monkeypatch.setattr(node_identity, "bound_owner", unavailable)
        else:
            monkeypatch.setattr(node_identity, "bound_owner", lambda: None if change == "unbound" else "oidc.other.owner")
        return caller

    monkeypatch.setattr(pending, "authenticate", changed_binding)
    with pytest.raises(AssignmentError) as refused:
        await invoke(registration, pending)
    assert refused.value.status_code == (503 if change == "unreadable" else 403)
    assert registration.registry.trust_for(BASE) is registration.original
