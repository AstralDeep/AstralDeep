"""Tests for owner-managed A2A publication: proposal/confirmation/withdrawal/expiry,
the shared discovery and callable policy, direct-invocation admission, foreign-owner
denials, durable Plane state adaptation, and the authenticated publication API.
"""

from __future__ import annotations

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


_identity = None


def _auth(sub=ALICE, email=ALICE_EMAIL, roles=("user",)) -> dict:
    return _identity.headers(
        sub, roles=roles, email=email, aud=["astral-agent-service", "account"]
    )


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


