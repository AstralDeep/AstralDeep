"""Tests for owner-managed A2A publication: proposal/confirmation/withdrawal/expiry,
the shared discovery and callable policy, direct-invocation admission, foreign-owner
denials, durable Plane state adaptation, and the authenticated publication API.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.a2a_publication import (  # noqa: E402
    A2APublicationRegistry,
    AlreadyWithdrawnError,
    ConfirmationMismatchError,
    InvalidAudienceError,
    InvalidLifetimeError,
    NotEligibleError,
    PublicationForbiddenError,
    PublicationStoreConflict,
    PublicationStoreUnavailable,
    StaleIntentError,
    UnknownCapabilityError,
    UnknownPublicationError,
    normalize_audiences,
)
from shared.protocol import MCPResponse  # noqa: E402

ADMIN = "admin-sub"
ALICE = "alice-sub"
BOB = "bob-sub"
ALICE_EMAIL = "alice@example.test"
BOB_EMAIL = "bob@example.test"


class _Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Store:
    def __init__(self, state=None):
        self.state = state
        self.saved = []
        self.fail_load = False
        self.fail_save = False

    def load(self):
        if self.fail_load:
            raise RuntimeError("store offline")
        return self.state

    def save(self, state, expected_version, event, *, authorize=None):
        if authorize is not None and not authorize(event["agent_id"]):
            raise PublicationForbiddenError("authority changed")
        if self.fail_save:
            raise RuntimeError("store offline")
        current = 0 if self.state is None else int(self.state.get("version", 0))
        if current != expected_version:
            raise PublicationStoreConflict("changed")
        self.state = dict(state)
        self.saved.append(self.state)


def _registry(clock=None, *, catalog=("weather-1", "web-research-1"),
              audiences=None, audit_sink=None, store=None):
    clock = clock or _Clock()
    audiences = audiences or {
        "weather-1": ("tools:read", "tools:search"),
        "web-research-1": ("tools:search",),
    }
    counter = {"n": 0}

    def _next_id():
        counter["n"] += 1
        return f"id-{counter['n']}"

    def _token():
        counter["n"] += 1
        return f"tok-{counter['n']}"

    registry = A2APublicationRegistry(
        catalog=lambda: catalog,
        audiences=lambda agent_id: audiences.get(agent_id, ()),
        time_fn=clock,
        id_factory=_next_id,
        token_factory=_token,
        audit_sink=audit_sink,
        store=store,
    )
    registry.clock = clock
    return registry


def _publish(registry, agent_id="weather-1", audiences=None, lifetime="24h", actor=ADMIN):
    intent = registry.create_intent(
        agent_id=agent_id,
        audiences=audiences or ["tools:read"],
        lifetime=lifetime,
        actor=actor,
    )
    return registry.confirm(
        intent_id=intent.intent_id,
        confirmation_token=intent.confirmation_token,
        actor=actor,
    )


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


def _claims(sub: str, email: str, roles=("user",), **extra) -> dict:
    claims = {
        "sub": sub,
        "preferred_username": email.split("@")[0],
        "email": email,
        "iss": "https://iam.example/realms/astral",
        "azp": "astral-frontend",
        "aud": ["astral-agent-service", "account"],
        "realm_access": {"roles": list(roles)},
    }
    claims.update(extra)
    return claims


def _auth(sub=ALICE, email=ALICE_EMAIL, roles=("user",)) -> dict:
    return {"Authorization": f"Bearer {_jwt(_claims(sub, email, roles))}"}


def _skill(skill_id: str, scope: str = "tools:read"):
    return SimpleNamespace(
        id=skill_id,
        name=skill_id,
        description=f"{skill_id} description",
        scope=scope,
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        metadata={},
    )


def _card(agent_id: str, name: str, skills):
    return SimpleNamespace(
        agent_id=agent_id, name=name, description=f"{name} agent", skills=skills
    )


class _Permissions:
    def __init__(self, safe_ids):
        self._safe = set(safe_ids)

    def list_disabled_agents(self, user_id):
        return ()

    def is_tool_allowed(self, user_id, agent_id, tool_name):
        if user_id == BOB and tool_name == "get_weather":
            return False
        return True

    def _is_safe_agent(self, agent_id):
        return agent_id in self._safe


def _orchestrator(*, safe=("weather-1",), owners=None):
    orch = SimpleNamespace()
    orch.agent_cards = {
        "weather-1": _card("weather-1", "Weather", [_skill("get_weather")]),
        "web-research-1": _card(
            "web-research-1", "Web Research", [_skill("web_search", "tools:search")]
        ),
        "draft-secret-9": _card(
            "draft-secret-9", "Secret Draft", [_skill("exfiltrate", "tools:write")]
        ),
    }
    orch.agents = {}
    orch.local_agents = {
        "weather-1": object(), "web-research-1": object(), "draft-secret-9": object(),
    }
    orch.security_flags = {}
    orch._is_draft_agent = lambda agent_id: agent_id == "draft-secret-9"
    orch.tool_permissions = _Permissions(safe_ids=safe)
    orch.a2a_agent_owners = dict(owners or {})
    orch.execute_authorized_tool = AsyncMock(
        return_value=MCPResponse(request_id="r1", result={"temp": 21})
    )
    from orchestrator.a2a_orchestrator_executor import public_catalog_agent_ids
    from orchestrator.a2a_publication_wiring import registered_audiences
    orch.a2a_publications = A2APublicationRegistry(
        catalog=lambda: public_catalog_agent_ids(orch),
        audiences=lambda agent_id: registered_audiences(orch, agent_id),
    )
    return orch


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.example/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_ALLOWED_AZP", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


def _client(orch):
    from orchestrator.a2a_orchestrator_executor import setup_orchestrator_a2a

    app = FastAPI()
    setup_orchestrator_a2a(app, orch)
    client = TestClient(app)
    client.orch = orch  # type: ignore[attr-defined]
    return client


V1 = {"A2A-Version": "1.0"}


def _send(client, headers, parts, rpc_id="1"):
    msg = {"message_id": f"m-{rpc_id}", "role": "ROLE_USER", "parts": parts}
    return client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": rpc_id, "method": "SendMessage",
              "params": {"message": msg}},
        headers={**V1, **headers},
    )


def _task(body: dict) -> dict:
    result = body["result"]
    return result.get("task", result)


def _result_parts(body: dict) -> list:
    task = _task(body)
    msg = task.get("status", {}).get("message") or task
    return msg.get("parts", [])


def _data_part(body: dict) -> dict:
    for part in _result_parts(body):
        if "data" in part:
            return part["data"]
    return {}


def _tool_names(body: dict) -> set:
    return {t["name"] for t in _data_part(body).get("tools", [])}


def _publish_for_client(client, agent_id="weather-1", actor=ALICE, audiences=None):
    registry = client.orch.a2a_publications
    intent = registry.create_intent(
        agent_id=agent_id,
        audiences=audiences or ["tools:read"],
        lifetime="open",
        actor=actor,
    )
    return registry.confirm(
        intent_id=intent.intent_id,
        confirmation_token=intent.confirmation_token,
        actor=actor,
    )




def test_publish_intent_confirm_flow_activates_publication():
    events = []
    registry = _registry(audit_sink=events.append)
    record = _publish(registry)
    assert record.status == "active"
    assert record.agent_id == "weather-1"
    assert record.audiences == ("tools:read",)
    assert registry.is_published("weather-1")
    assert registry.active_agent_ids() == ("weather-1",)
    assert [event["action"] for event in events] == ["intent_created", "published"]


def test_confirmation_token_mismatch_denies_and_consumes_intent():
    events = []
    registry = _registry(audit_sink=events.append)
    intent = registry.create_intent(
        agent_id="weather-1", audiences=["tools:read"], lifetime="24h", actor=ADMIN,
    )
    with pytest.raises(ConfirmationMismatchError):
        registry.confirm(
            intent_id=intent.intent_id, confirmation_token="wrong", actor=ADMIN,
        )
    with pytest.raises(UnknownPublicationError):
        registry.confirm(
            intent_id=intent.intent_id, confirmation_token=intent.confirmation_token,
            actor=ADMIN,
        )
    assert registry.active() == ()
    assert any(event["action"] == "confirm_denied" for event in events)


def test_intent_expires_and_recovery_creates_new_intent():
    registry = _registry()
    intent = registry.create_intent(
        agent_id="weather-1", audiences=["tools:read"], lifetime="24h", actor=ADMIN,
    )
    registry.clock.advance(601)
    with pytest.raises(StaleIntentError):
        registry.confirm(
            intent_id=intent.intent_id,
            confirmation_token=intent.confirmation_token,
            actor=ADMIN,
        )
    record = _publish(registry)
    assert record.status == "active"


def test_publication_lifetime_expiry_is_enforced_at_read():
    registry = _registry()
    record = _publish(registry, lifetime="1h")
    assert record.expires_at == registry.clock.now + 3_600
    assert registry.is_published("weather-1")
    registry.clock.advance(3_599)
    assert registry.is_published("weather-1")
    registry.clock.advance(2)
    assert not registry.is_published("weather-1")
    assert registry.active() == ()
    statuses = {entry["publication_id"]: entry["status"] for entry in registry.inventory()}
    assert statuses[record.publication_id] == "expired"
    with pytest.raises(AlreadyWithdrawnError):
        registry.withdraw(publication_id=record.publication_id, actor=ADMIN)


def test_open_ended_publication_has_no_expiry():
    registry = _registry()
    record = _publish(registry, lifetime="open")
    assert record.expires_at is None
    registry.clock.advance(10_000_000)
    assert registry.is_published("weather-1")


def test_withdraw_removes_advertisement_and_keeps_inventory():
    events = []
    registry = _registry(audit_sink=events.append)
    record = _publish(registry)
    withdrawn = registry.withdraw(publication_id=record.publication_id, actor=ADMIN)
    assert withdrawn.status == "withdrawn"
    assert withdrawn.withdrawn_by == ADMIN
    assert not registry.is_published("weather-1")
    entries = {e["publication_id"]: e for e in registry.inventory()}
    assert entries[record.publication_id]["status"] == "withdrawn"
    assert [event["action"] for event in events][-1] == "withdrawn"


def test_foreign_owner_cannot_withdraw_or_confirm():
    events = []
    registry = _registry(audit_sink=events.append)
    record = _publish(registry, actor=ALICE)
    with pytest.raises(PublicationForbiddenError):
        registry.withdraw(publication_id=record.publication_id, actor=BOB)
    assert registry.is_published("weather-1")
    intent = registry.create_intent(
        agent_id="weather-1", audiences=["tools:read"], lifetime="24h", actor=ALICE,
    )
    with pytest.raises(PublicationForbiddenError):
        registry.confirm(
            intent_id=intent.intent_id,
            confirmation_token=intent.confirmation_token,
            actor=BOB,
        )
    assert any(event["action"] == "withdraw_denied" for event in events)


def test_double_withdraw_and_unknown_publication_are_denied():
    registry = _registry()
    record = _publish(registry)
    registry.withdraw(publication_id=record.publication_id, actor=ADMIN)
    with pytest.raises(AlreadyWithdrawnError):
        registry.withdraw(publication_id=record.publication_id, actor=ADMIN)
    with pytest.raises(UnknownPublicationError):
        registry.withdraw(publication_id="nope", actor=ADMIN)


def test_republish_after_withdraw_is_a_new_publication():
    registry = _registry()
    first = _publish(registry)
    registry.withdraw(publication_id=first.publication_id, actor=ADMIN)
    second = _publish(registry)
    assert second.publication_id != first.publication_id
    assert registry.is_published("weather-1")


def test_intent_denied_for_unknown_or_ineligible_capability():
    registry = _registry()
    with pytest.raises(UnknownCapabilityError):
        registry.create_intent(
            agent_id=" ", audiences=["tools:read"], lifetime="24h", actor=ADMIN,
        )
    with pytest.raises(NotEligibleError):
        registry.create_intent(
            agent_id="draft-secret-9", audiences=["tools:write"], lifetime="24h",
            actor=ADMIN,
        )


def test_intent_denied_when_capability_has_no_registered_audiences():
    registry = _registry(audiences={"weather-1": ("tools:read",), "web-research-1": ()})
    with pytest.raises(NotEligibleError):
        registry.create_intent(
            agent_id="web-research-1", audiences=["tools:search"], lifetime="24h",
            actor=ADMIN,
        )


def test_intent_denied_for_unregistered_audience_or_lifetime():
    registry = _registry()
    with pytest.raises(InvalidAudienceError):
        registry.create_intent(
            agent_id="weather-1", audiences=["tools:system"], lifetime="24h", actor=ADMIN,
        )
    with pytest.raises(InvalidLifetimeError):
        registry.create_intent(
            agent_id="weather-1", audiences=["tools:read"], lifetime="forever",
            actor=ADMIN,
        )
    with pytest.raises(InvalidAudienceError):
        registry.create_intent(
            agent_id="weather-1", audiences=[], lifetime="24h", actor=ADMIN,
        )


def test_audience_normalization_accepts_string_and_dedupes():
    assert normalize_audiences("tools:read") == ("tools:read",)
    assert normalize_audiences(["tools:read", "tools:read", "tools:search"]) == (
        "tools:read", "tools:search",
    )
    with pytest.raises(InvalidAudienceError):
        normalize_audiences(["tools read"])
    with pytest.raises(InvalidAudienceError):
        normalize_audiences(42)


def test_confirm_denied_when_capability_leaves_catalog_between_intent_and_confirm():
    registry = _registry()
    intent = registry.create_intent(
        agent_id="weather-1", audiences=["tools:read"], lifetime="24h", actor=ADMIN,
    )

    def _narrow_catalog():
        return ("web-research-1",)

    registry._catalog = _narrow_catalog
    with pytest.raises(NotEligibleError):
        registry.confirm(
            intent_id=intent.intent_id,
            confirmation_token=intent.confirmation_token,
            actor=ADMIN,
        )
    assert registry.active() == ()


def test_inventory_and_pending_are_sanitized_of_confirmation_tokens():
    registry = _registry()
    intent = registry.create_intent(
        agent_id="weather-1", audiences=["tools:read"], lifetime="24h", actor=ADMIN,
    )
    record = registry.confirm(
        intent_id=intent.intent_id,
        confirmation_token=intent.confirmation_token,
        actor=ADMIN,
    )
    blob = json.dumps(registry.inventory()) + json.dumps(registry.pending_intents())
    assert intent.confirmation_token not in blob
    assert record.publication_id in blob


def test_eligible_catalog_reports_published_state():
    registry = _registry()
    _publish(registry)
    entries = {agent_id: published for agent_id, _aud, published in registry.eligible_catalog()}
    assert entries["weather-1"] is True
    assert entries["web-research-1"] is False


def test_advertised_audiences_union_across_publications():
    registry = _registry()
    _publish(registry, audiences=["tools:read"])
    _publish(registry, agent_id="web-research-1", audiences=["tools:search"])
    assert registry.advertised_audiences() == ("tools:read", "tools:search")
    assert registry.audiences_for("weather-1") == ("tools:read",)


def test_pending_intent_bound_excludes_other_actors():
    registry = _registry()
    registry.create_intent(
        agent_id="weather-1", audiences=["tools:read"], lifetime="24h", actor="someone-else",
    )
    assert registry.pending_intents(actor=ADMIN) == ()
    assert len(registry.pending_intents()) == 1


def test_registry_persists_lifecycle_and_recovers_after_restart():
    store = _Store()
    first = _registry(store=store)
    record = _publish(first, lifetime="1h")
    assert store.state["version"] == 2
    second = _registry(store=store)
    assert second.refresh() is True
    assert second.is_published("weather-1")
    assert second.inventory()[0]["publication_id"] == record.publication_id
    second.withdraw(publication_id=record.publication_id, actor=ADMIN)
    third = _registry(store=store)
    assert third.refresh() is True
    assert not third.is_published("weather-1")
    assert third.inventory()[0]["status"] == "withdrawn"


def test_registry_store_failure_fails_closed():
    store = _Store()
    store.fail_load = True
    registry = _registry(store=store)
    assert registry.refresh() is False
    assert registry.available is False
    assert registry.active() == ()
    with pytest.raises(PublicationStoreUnavailable):
        registry.create_intent(
            agent_id="weather-1", audiences=["tools:read"], lifetime="24h", actor=ADMIN,
        )


def test_registry_store_save_failure_does_not_change_in_memory_state():
    store = _Store()
    registry = _registry(store=store)
    store.fail_save = True
    with pytest.raises(PublicationStoreUnavailable):
        registry.create_intent(
            agent_id="weather-1", audiences=["tools:read"], lifetime="24h", actor=ADMIN,
        )
    assert registry.pending_intents() == ()




def test_card_without_registry_keeps_safe_catalog_behavior(mock_env):
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card

    orch = _orchestrator()
    del orch.a2a_publications
    card = build_orchestrator_a2a_card(orch)
    ids = {skill.id for skill in card.skills}
    assert "weather-1" in ids
    assert "draft-secret-9" not in ids


def test_card_with_empty_registry_advertises_only_chat(mock_env):
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    orch = _orchestrator()
    attach_publication_registry(orch)
    card = build_orchestrator_a2a_card(orch)
    ids = {skill.id for skill in card.skills}
    assert ids == {"chat"}
    assert card.security_requirements in (None, [])


def test_card_advertises_published_capability_with_chosen_audience(mock_env):
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    orch = _orchestrator()
    registry = attach_publication_registry(orch)
    _publish(registry, audiences=["tools:read"])
    card = build_orchestrator_a2a_card(orch)
    ids = {skill.id for skill in card.skills}
    assert "weather-1" in ids
    assert "draft-secret-9" not in ids
    assert "chat" in ids
    blob = repr(card.skills)
    assert "draft-secret-9" not in blob
    requirements = repr(card.security_requirements)
    assert "tools:read" in requirements


def test_card_drops_withdrawn_and_expired_capabilities(mock_env):
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card

    orch = _orchestrator()
    registry = _registry()
    orch.a2a_publications = registry
    first = _publish(registry, lifetime="1h")
    assert "weather-1" in {skill.id for skill in build_orchestrator_a2a_card(orch).skills}
    registry.clock.advance(3_601)
    assert {skill.id for skill in build_orchestrator_a2a_card(orch).skills} == {"chat"}
    second = _publish(registry, lifetime="open")
    registry.withdraw(publication_id=second.publication_id, actor=ADMIN)
    assert {skill.id for skill in build_orchestrator_a2a_card(orch).skills} == {"chat"}
    assert registry.get(first.publication_id).status == "active"


def test_card_intersection_keeps_unsafe_agents_unadvertised(mock_env):
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    orch = _orchestrator()
    registry = attach_publication_registry(orch)
    _publish(registry)
    orch.tool_permissions._safe.discard("weather-1")
    ids = {skill.id for skill in build_orchestrator_a2a_card(orch).skills}
    assert "weather-1" not in ids
    assert "chat" in ids




def test_tools_list_hides_unpublished_tools_and_shows_published(mock_env):
    orch = _orchestrator(safe=("weather-1", "web-research-1"))
    client = _client(orch)
    before = _send(client, _auth(ALICE), [{"data": {"method": "tools/list"}}], rpc_id="l1")
    assert _tool_names(before.json()) == set()

    _publish_for_client(client, "weather-1")
    after = _send(client, _auth(ALICE), [{"data": {"method": "tools/list"}}], rpc_id="l2")
    assert _tool_names(after.json()) == {"get_weather"}


def test_unmanaged_dynamic_agent_keeps_legacy_authenticated_behavior(mock_env):
    orch = _orchestrator()
    orch.agent_cards["dynamic-1"] = _card(
        "dynamic-1", "Dynamic", [_skill("dynamic_lookup", "tools:read")],
    )
    orch.local_agents["dynamic-1"] = object()
    client = _client(orch)
    listed = _send(client, _auth(ALICE), [{"data": {"method": "tools/list"}}], rpc_id="d1")
    assert _tool_names(listed.json()) == {"dynamic_lookup"}
    response = _send(
        client, _auth(ALICE),
        [{"data": {"method": "tools/call", "name": "dynamic_lookup", "arguments": {}}}],
        rpc_id="d2",
    )
    assert _task(response.json())["status"]["state"] == "TASK_STATE_COMPLETED"
    assert orch.execute_authorized_tool.await_args.kwargs["agent_id"] == "dynamic-1"


def test_unpublished_direct_tool_is_non_disclosing_and_never_dispatched(mock_env):
    orch = _orchestrator()
    client = _client(orch)
    response = _send(
        client, _auth(ALICE),
        [{"data": {"method": "tools/call", "name": "get_weather", "arguments": {}}}],
        rpc_id="c1",
    )
    assert response.status_code == 200
    assert _task(response.json())["status"]["state"] == "TASK_STATE_FAILED"
    texts = [p.get("text", "") for p in _result_parts(response.json())]
    assert any("unavailable or not authorized" in text for text in texts)
    orch.execute_authorized_tool.assert_not_awaited()


def test_stale_card_invocation_is_refused_after_withdrawal(mock_env):
    orch = _orchestrator()
    client = _client(orch)
    record = _publish_for_client(client)
    card = client.get("/a2a/.well-known/agent-card.json").json()
    assert "weather-1" in {skill["id"] for skill in card["skills"]}
    client.orch.a2a_publications.withdraw(
        publication_id=record.publication_id, actor=ALICE,
    )
    response = _send(
        client, _auth(ALICE),
        [{"data": {"method": "tools/call", "name": "get_weather", "arguments": {}}}],
        rpc_id="c2",
    )
    assert _task(response.json())["status"]["state"] == "TASK_STATE_FAILED"
    orch.execute_authorized_tool.assert_not_awaited()


def test_published_direct_tool_uses_authorized_dispatch(mock_env):
    orch = _orchestrator()
    client = _client(orch)
    _publish_for_client(client)
    response = _send(
        client, _auth(ALICE),
        [{"data": {"method": "tools/call", "name": "get_weather", "arguments": {"city": "Lexington"}}}],
        rpc_id="c3",
    )
    assert response.status_code == 200
    assert _task(response.json())["status"]["state"] == "TASK_STATE_COMPLETED"
    kwargs = orch.execute_authorized_tool.await_args.kwargs
    assert kwargs["agent_id"] == "weather-1"
    assert kwargs["tool_name"] == "get_weather"
    assert kwargs["channel"] == "a2a"


def test_withdrawal_during_admitted_call_does_not_cancel_in_flight_work(mock_env):
    orch = _orchestrator()
    client = _client(orch)
    record = _publish_for_client(client)

    async def _dispatch(**_kwargs):
        client.orch.a2a_publications.withdraw(
            publication_id=record.publication_id, actor=ALICE,
        )
        return MCPResponse(request_id="r1", result={"temp": 21})

    orch.execute_authorized_tool = AsyncMock(side_effect=_dispatch)
    response = _send(
        client, _auth(ALICE),
        [{"data": {"method": "tools/call", "name": "get_weather", "arguments": {}}}],
        rpc_id="c4",
    )
    assert _task(response.json())["status"]["state"] == "TASK_STATE_COMPLETED"
    assert orch.execute_authorized_tool.await_count == 1
    assert not client.orch.a2a_publications.is_published("weather-1")


def test_policy_admission_records_publication_snapshot(mock_env):
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    orch = _orchestrator()
    registry = attach_publication_registry(orch)
    record = _publish(registry)
    admission = orch.a2a_publication_policy.admit("weather-1")
    assert admission.publication_id == record.publication_id
    assert admission.audiences == ("tools:read",)
    registry.withdraw(publication_id=record.publication_id, actor=ADMIN)
    assert admission.publication_id == record.publication_id


def test_policy_denial_is_audited(mock_env):
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    events = []
    orch = _orchestrator()
    registry = attach_publication_registry(orch)
    registry.record_event = events.append
    assert orch.a2a_publication_policy.admit("weather-1") is None
    orch.a2a_publication_policy.record_denial(
        actor=ALICE, agent_id="weather-1", reason="not_published",
    )
    assert events == [{
        "action": "invocation_denied",
        "agent_id": "weather-1",
        "actor": ALICE,
        "reason": "not_published",
    }]




class _Canvas:
    def __init__(self):
        self.record = None

    def get_scoped(self, transaction, *, owner_id, conversation_id, component_id):
        return self.record

    def create(self, transaction, record):
        self.record = SimpleNamespace(
            payload=dict(record.payload),
            updated_at=record.updated_at,
        )
        return self.record

    def replace(self, transaction, *, owner_id, conversation_id, component_id,
                payload, component_type, title, expected_updated_at, updated_at,
                publication_id=None, committed_render_revision=None):
        if self.record is None or self.record.updated_at != expected_updated_at:
            return None
        self.record = SimpleNamespace(payload=dict(payload), updated_at=updated_at)
        return self.record


class _Conversations:
    def create(self, transaction, **kwargs):
        return SimpleNamespace(**kwargs)


class _Runtime:
    class _Transaction:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    def transaction(self):
        return self._Transaction()


class _Repositories:
    def __init__(self):
        self.history = SimpleNamespace(conversations=_Conversations())
        self.workspaces = SimpleNamespace(canvas=_Canvas())




def _api_client(orch):
    from orchestrator.a2a_publication_api import publication_router
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    attach_publication_registry(orch)
    app = FastAPI()
    app.include_router(publication_router)
    app.state.orchestrator = orch
    return TestClient(app)


def test_api_requires_authentication(mock_env):
    client = _api_client(_orchestrator())
    for method, url in (("get", "/api/a2a/publications"),
                        ("get", "/api/a2a/publications/catalog")):
        response = getattr(client, method)(url)
        assert response.status_code == 401, url


def test_api_full_owner_publish_inventory_withdraw_cycle(mock_env):
    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    client = _api_client(orch)
    headers = _auth()

    catalog = client.get("/api/a2a/publications/catalog", headers=headers).json()
    capabilities = {c["agent_id"]: c for c in catalog["capabilities"]}
    assert capabilities["weather-1"]["audiences"] == ["tools:read"]
    assert capabilities["weather-1"]["published"] is False
    assert capabilities["weather-1"]["owner"] == ALICE_EMAIL
    assert "30d" in catalog["lifetimes"]

    intent_response = client.post(
        "/api/a2a/publications/intents",
        headers=headers,
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "7d"},
    )
    assert intent_response.status_code == 200, intent_response.text
    intent = intent_response.json()
    assert intent["confirmation_token"]

    confirm = client.post(
        f"/api/a2a/publications/intents/{intent['intent_id']}/confirm",
        headers=headers,
        json={"confirmation_token": intent["confirmation_token"]},
    )
    assert confirm.status_code == 200, confirm.text
    publication = confirm.json()
    assert publication["status"] == "active"

    inventory = client.get("/api/a2a/publications", headers=headers).json()
    ids = {p["publication_id"] for p in inventory["publications"]}
    assert publication["publication_id"] in ids
    assert intent["confirmation_token"] not in json.dumps(inventory)

    withdrawn = client.delete(
        f"/api/a2a/publications/{publication['publication_id']}",
        headers=headers,
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "withdrawn"

    capabilities = {
        c["agent_id"]: c
        for c in client.get(
            "/api/a2a/publications/catalog", headers=headers,
        ).json()["capabilities"]
    }
    assert capabilities["weather-1"]["published"] is False


def test_api_foreign_owner_cannot_manage_and_admin_can(mock_env):
    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    client = _api_client(orch)
    foreign = _auth(BOB, BOB_EMAIL)
    admin = _auth(ADMIN, "admin@example.test", roles=("admin",))

    catalog = client.get("/api/a2a/publications/catalog", headers=foreign).json()
    assert catalog["capabilities"] == []

    denied = client.post(
        "/api/a2a/publications/intents",
        headers=foreign,
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "1h"},
    )
    assert denied.status_code == 403

    owner = client.post(
        "/api/a2a/publications/intents",
        headers=_auth(),
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "1h"},
    ).json()
    confirmed = client.post(
        f"/api/a2a/publications/intents/{owner['intent_id']}/confirm",
        headers=_auth(),
        json={"confirmation_token": owner["confirmation_token"]},
    ).json()
    foreign_withdraw = client.delete(
        f"/api/a2a/publications/{confirmed['publication_id']}", headers=foreign,
    )
    assert foreign_withdraw.status_code == 403
    admin_withdraw = client.delete(
        f"/api/a2a/publications/{confirmed['publication_id']}", headers=admin,
    )
    assert admin_withdraw.status_code == 200


def test_api_intent_rejects_unregistered_audience(mock_env):
    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    client = _api_client(orch)
    response = client.post(
        "/api/a2a/publications/intents",
        headers=_auth(),
        json={"agent_id": "weather-1", "audiences": ["tools:system"], "lifetime": "7d"},
    )
    assert response.status_code == 400


def test_api_confirm_with_wrong_token_is_forbidden_and_stale_is_conflict(mock_env):
    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    client = _api_client(orch)
    clock = _Clock()
    client.app.state.orchestrator.a2a_publications._time_fn = clock
    headers = _auth()
    intent = client.post(
        "/api/a2a/publications/intents", headers=headers,
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "1h"},
    ).json()
    response = client.post(
        f"/api/a2a/publications/intents/{intent['intent_id']}/confirm",
        headers=headers,
        json={"confirmation_token": "bad"},
    )
    assert response.status_code == 403

    stale = client.post(
        "/api/a2a/publications/intents", headers=headers,
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "1h"},
    ).json()
    clock.advance(601)
    response = client.post(
        f"/api/a2a/publications/intents/{stale['intent_id']}/confirm",
        headers=headers,
        json={"confirmation_token": stale["confirmation_token"]},
    )
    assert response.status_code == 409


def test_api_withdraw_unknown_is_not_found_and_double_is_conflict(mock_env):
    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    client = _api_client(orch)
    headers = _auth()
    assert client.delete("/api/a2a/publications/missing", headers=headers).status_code == 404
    intent = client.post(
        "/api/a2a/publications/intents", headers=headers,
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "1h"},
    ).json()
    confirmed = client.post(
        f"/api/a2a/publications/intents/{intent['intent_id']}/confirm",
        headers=headers, json={"confirmation_token": intent["confirmation_token"]},
    ).json()
    assert client.delete(
        f"/api/a2a/publications/{confirmed['publication_id']}", headers=headers,
    ).status_code == 200
    assert client.delete(
        f"/api/a2a/publications/{confirmed['publication_id']}", headers=headers,
    ).status_code == 409


def test_api_disabled_without_registry_returns_conflict(mock_env):
    from orchestrator.a2a_publication_api import publication_router

    app = FastAPI()
    app.include_router(publication_router)
    app.state.orchestrator = _orchestrator()
    del app.state.orchestrator.a2a_publications
    client = TestClient(app)
    response = client.get("/api/a2a/publications", headers=_auth())
    assert response.status_code == 409
    assert "not enabled" in response.json()["detail"]


def test_api_orchestrator_unavailable_returns_503(mock_env):
    from orchestrator.a2a_publication_api import publication_router

    app = FastAPI()
    app.include_router(publication_router)
    client = TestClient(app)
    response = client.get("/api/a2a/publications", headers=_auth())
    assert response.status_code == 503


def test_api_success_has_one_lifecycle_event(mock_env):
    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    client = _api_client(orch)
    recorded = []
    orch.a2a_publications._audit_sink = recorded.append
    client.post(
        "/api/a2a/publications/intents", headers=_auth(),
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "7d"},
    )
    assert [event["action"] for event in recorded] == ["intent_created"]


def test_publication_wiring_requires_durable_runtime(mock_env):
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    orch = _orchestrator()
    del orch.a2a_publications
    with pytest.raises(Exception):
        attach_publication_registry(orch)


def test_policy_fails_closed_when_catalog_and_registry_reads_fail(mock_env):
    from orchestrator.a2a_publication_policy import A2APublicationPolicy
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    orch = _orchestrator()
    registry = attach_publication_registry(orch)
    _publish(registry)

    def _broken():
        raise RuntimeError("catalog unavailable")

    policy = A2APublicationPolicy(orch, registry, catalog=_broken, managed=_broken)
    assert policy.catalog_ids() == ()
    assert policy.managed_ids() == frozenset()
    assert policy.admit("weather-1") is None
    assert policy.published_records() == ()
    assert policy.filter_projected_tools(()) == ()

    registry.active = lambda: (_ for _ in ()).throw(RuntimeError("state unavailable"))
    readable = A2APublicationPolicy(
        orch, registry, catalog=lambda: ("weather-1",),
    )
    assert readable.published_records() == ()


def test_policy_admits_framework_work_tools_without_publication(mock_env):
    from orchestrator.a2a_publication_wiring import attach_publication_registry
    from orchestrator.mcp_projection import FRAMEWORK_WORK_AGENT_ID

    orch = _orchestrator()
    attach_publication_registry(orch)
    admission = orch.a2a_publication_policy.admit(FRAMEWORK_WORK_AGENT_ID)
    assert admission.publication_id == "framework-work-tools"
    assert admission.audiences == ()


def test_policy_owner_management_and_lookup_failure(mock_env):
    from orchestrator.a2a_publication_policy import A2APublicationPolicy
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    orch = _orchestrator()
    registry = attach_publication_registry(orch)
    policy = A2APublicationPolicy(
        orch,
        registry,
        catalog=lambda: ("weather-1",),
        owner_lookup=lambda agent_id: "owner@example.test",
    )
    assert policy.owner_of("weather-1") == "owner@example.test"
    assert policy.can_manage({"owner@example.test"}, "weather-1")
    assert not policy.can_manage({"other@example.test"}, "weather-1")
    assert not policy.can_manage({"owner@example.test"}, "missing-1")
    assert policy.can_manage(set(), "weather-1", is_admin=True) is True
    assert policy.can_manage(set(), "missing-1", is_admin=True) is False

    def _boom(agent_id):
        raise RuntimeError("owner unavailable")

    broken = A2APublicationPolicy(
        orch, registry, catalog=lambda: ("weather-1",), owner_lookup=_boom,
    )
    with pytest.raises(PublicationStoreUnavailable):
        broken.owner_of("weather-1")
    assert not broken.can_manage(set(), "weather-1", is_admin=True)
    with pytest.raises(PublicationStoreUnavailable):
        A2APublicationPolicy(
            orch, registry, catalog=lambda: ("weather-1",),
        ).owner_of("weather-1")


def test_wiring_resolves_plane_owner_and_store_and_reuses_registry(mock_env):
    from orchestrator.a2a_publication_policy import A2APublicationPolicy
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    repositories = _Repositories()
    repositories.agents = SimpleNamespace(
        get_ownership=lambda transaction, agent_id: SimpleNamespace(
            owner_email="plane-owner@example.test"
        )
    )
    orch = _orchestrator()
    del orch.a2a_agent_owners
    orch.plane_repository_source = SimpleNamespace(
        plane_runtime=_Runtime(), plane_repositories=repositories,
    )
    registry = attach_publication_registry(orch)
    assert attach_publication_registry(orch) is registry
    assert isinstance(orch.a2a_publication_policy, A2APublicationPolicy)
    assert orch.a2a_publication_policy.owner_of("weather-1") == "plane-owner@example.test"
    assert registry.available is True


def test_wiring_supports_callable_owner_lookup(mock_env):
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    orch = _orchestrator()
    orch.a2a_agent_owners = lambda agent_id: "callable-owner@example.test"
    attach_publication_registry(orch)
    assert orch.a2a_publication_policy.owner_of("weather-1") == "callable-owner@example.test"


def test_card_skips_a_publication_whose_card_vanished(mock_env):
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card
    from orchestrator.a2a_publication_wiring import attach_publication_registry

    orch = _orchestrator()
    registry = attach_publication_registry(orch)
    _publish(registry)
    orch.agent_cards.pop("weather-1")
    assert {skill.id for skill in build_orchestrator_a2a_card(orch).skills} == {"chat"}


def test_api_audit_failure_does_not_block_lifecycle(mock_env, monkeypatch):
    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    client = _api_client(orch)

    class _BrokenRecorder:
        async def record(self, event):
            raise RuntimeError("audit unavailable")

    from audit import recorder as audit_recorder

    monkeypatch.setattr(audit_recorder, "get_recorder", lambda: _BrokenRecorder())
    response = client.post(
        "/api/a2a/publications/intents", headers=_auth(),
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "1h"},
    )
    assert response.status_code == 200


def test_api_policy_fallback_and_owner_denial_paths(mock_env):
    from orchestrator.a2a_publication_api import _policy

    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    _api_client(orch)
    del orch.a2a_publication_policy
    policy = _policy(orch)
    assert policy is not None
    assert policy.can_manage({ALICE_EMAIL}, "weather-1")
    empty = _orchestrator()
    del empty.a2a_publications
    assert _policy(empty) is None


def test_api_returns_service_unavailable_when_publication_state_is_degraded(mock_env):
    from orchestrator.a2a_publication_api import publication_router
    from shared.a2a_publication import A2APublicationRegistry

    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    registry = A2APublicationRegistry(
        catalog=lambda: ("weather-1",),
        audiences=lambda agent_id: ("tools:read",),
        store=_Store(),
    )
    registry._store.fail_load = True
    registry.refresh()
    orch.a2a_publications = registry
    app = FastAPI()
    app.include_router(publication_router)
    app.state.orchestrator = orch
    response = TestClient(app).get("/api/a2a/publications", headers=_auth())
    assert response.status_code == 503


def test_registry_store_conflict_is_recoverable_without_corrupting_memory():
    store = _Store()
    registry = _registry(store=store)
    assert registry.refresh() is True
    store.state = {
        "format": "astral.a2a-publication-state/v1",
        "version": 1,
        "intents": [],
        "publications": [],
    }
    intent = registry.create_intent(
        agent_id="weather-1", audiences=["tools:read"], lifetime="24h", actor=ADMIN,
    )
    assert intent.agent_id == "weather-1"
    assert store.state["version"] == 2


def test_admin_invalid_capability_is_validation_error_not_owner_denial(mock_env):
    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    client = _api_client(orch)
    response = client.post(
        "/api/a2a/publications/intents",
        headers=_auth(ADMIN, "admin@example.test", roles=("admin",)),
        json={"agent_id": "draft-secret-9", "audiences": ["tools:write"], "lifetime": "1h"},
    )
    assert response.status_code == 400
