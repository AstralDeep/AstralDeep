"""Tests for shared selective A2A publication controls (shared/a2a_publication.py,
a2a_publication_api.py, the admin_tools a2a tab, and the card advertisement gating in
a2a_orchestrator_executor.py): publish intent/confirm/withdraw flows, expiry, denials,
sanitized inventory, audit events, and advertisement semantics.
"""

from __future__ import annotations

import base64
import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    StaleIntentError,
    UnknownCapabilityError,
    UnknownPublicationError,
    normalize_audiences,
)

ADMIN = "admin-sub"


class _Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _registry(clock=None, *, catalog=("weather-1", "web-research-1"),
              audiences=None, audit_sink=None):
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


def _admin_headers() -> dict:
    return {"Authorization": "Bearer dev-token"}


def _user_headers(admin: bool = False) -> dict:
    claims = {
        "sub": "plain-sub",
        "preferred_username": "plain",
        "iss": "https://iam.example/realms/astral",
        "azp": "astral-frontend",
        "aud": ["astral-agent-service", "account"],
        "realm_access": {"roles": ["admin"] if admin else ["user"]},
    }
    return {"Authorization": f"Bearer {_jwt(claims)}"}


def _skill(skill_id: str, scope: str = "tools:read"):
    return SimpleNamespace(
        id=skill_id, name=skill_id, description=f"{skill_id} description",
        scope=scope, input_schema={"type": "object", "properties": {}},
        output_schema=None, metadata={},
    )


def _card(agent_id: str, name: str, skills):
    return SimpleNamespace(
        agent_id=agent_id, name=name, description=f"{name} agent", skills=skills
    )


def _orchestrator():
    orch = SimpleNamespace()
    orch.agent_cards = {
        "weather-1": _card("weather-1", "Weather", [_skill("get_weather", "tools:read")]),
        "web-research-1": _card(
            "web-research-1", "Web Research", [_skill("web_search", "tools:search")]
        ),
        "draft-secret-9": _card("draft-secret-9", "Secret Draft", [_skill("exfiltrate", "tools:write")]),
    }
    orch.agents = {}
    orch.local_agents = {
        "weather-1": object(), "web-research-1": object(), "draft-secret-9": object(),
    }
    orch.security_flags = {}
    orch._is_draft_agent = lambda agent_id: False

    class _Permissions:
        def __init__(self):
            self._safe = {"weather-1"}

        def _is_safe_agent(self, agent_id):
            return agent_id in self._safe

    orch.tool_permissions = _Permissions()
    orch.execute_authorized_tool = AsyncMock(
        return_value=SimpleNamespace(result={"temp": 21}, error=None, ui_components=None)
    )
    return orch


# --- registry contract -------------------------------------------------------


def test_publish_intent_confirm_flow_activates_publication():
    events = []
    registry = _registry(audit_sink=events.append)
    record = _publish(registry)
    assert record.status == "active"
    assert record.agent_id == "weather-1"
    assert record.audiences == ("tools:read",)
    assert registry.is_published("weather-1")
    assert registry.active_agent_ids() == ("weather-1",)
    actions = [event["action"] for event in events]
    assert actions == ["intent_created", "published"]


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


def test_open_ended_publication_has_no_expiry():
    registry = _registry()
    record = _publish(registry, lifetime="open")
    assert record.expires_at is None
    registry.clock.advance(10_000_000)
    assert registry.is_published("weather-1")


def test_withdraw_removes_advertisement_and_keeps_inventory():
    registry = _registry()
    record = _publish(registry)
    withdrawn = registry.withdraw(publication_id=record.publication_id, actor=ADMIN)
    assert withdrawn.status == "withdrawn"
    assert withdrawn.withdrawn_by == ADMIN
    assert not registry.is_published("weather-1")
    entries = {e["publication_id"]: e for e in registry.inventory()}
    assert entries[record.publication_id]["status"] == "withdrawn"


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
            agent_id="weather-1", audiences=["tools:read"], lifetime="forever", actor=ADMIN,
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


# --- card advertisement ------------------------------------------------------


def test_card_without_registry_keeps_safe_catalog_behavior():
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card

    card = build_orchestrator_a2a_card(_orchestrator())
    ids = {skill.id for skill in card.skills}
    assert "weather-1" in ids
    assert "draft-secret-9" not in ids


def test_card_with_empty_registry_advertises_only_chat():
    from orchestrator.a2a_orchestrator_executor import (
        attach_publication_registry,
        build_orchestrator_a2a_card,
    )

    orch = _orchestrator()
    attach_publication_registry(orch)
    card = build_orchestrator_a2a_card(orch)
    ids = {skill.id for skill in card.skills}
    assert ids == {"chat"}
    assert card.security_requirements in (None, [])


def test_card_advertises_published_capability_with_chosen_audience(monkeypatch):
    from orchestrator.a2a_orchestrator_executor import (
        attach_publication_registry,
        build_orchestrator_a2a_card,
    )

    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.example/realms/astral")
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


def test_card_drops_withdrawn_capability():
    from orchestrator.a2a_orchestrator_executor import (
        attach_publication_registry,
        build_orchestrator_a2a_card,
    )

    orch = _orchestrator()
    registry = attach_publication_registry(orch)
    record = _publish(registry)
    registry.withdraw(publication_id=record.publication_id, actor=ADMIN)
    ids = {skill.id for skill in build_orchestrator_a2a_card(orch).skills}
    assert ids == {"chat"}


def test_card_drops_expired_publication():
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card

    orch = _orchestrator()
    registry = _registry()
    orch.a2a_publications = registry
    record = _publish(registry, lifetime="1h")
    registry.clock.advance(3_601)
    ids = {skill.id for skill in build_orchestrator_a2a_card(orch).skills}
    assert ids == {"chat"}
    assert registry.inventory()[0]["publication_id"] == record.publication_id


def test_card_intersection_keeps_unsafe_agents_unadvertised():
    from orchestrator.a2a_orchestrator_executor import (
        attach_publication_registry,
        build_orchestrator_a2a_card,
    )

    orch = _orchestrator()
    registry = attach_publication_registry(orch)
    _publish(registry)
    orch.tool_permissions._safe.discard("weather-1")
    ids = {skill.id for skill in build_orchestrator_a2a_card(orch).skills}
    assert "weather-1" not in ids
    assert "chat" in ids


def _executor_client(orch):
    from orchestrator.a2a_orchestrator_executor import setup_orchestrator_a2a

    app = FastAPI()
    setup_orchestrator_a2a(app, orch)
    client = TestClient(app)
    client.orch = orch  # type: ignore[attr-defined]
    return client


def _tool_call(client, name="get_weather", headers=None, rpc_id="c1"):
    claims = {
        "sub": "caller-sub", "iss": "https://iam.example/realms/astral",
        "azp": "astral-frontend", "realm_access": {"roles": ["user"]},
    }
    message = {
        "message_id": f"m-{rpc_id}", "role": "ROLE_USER",
        "parts": [{"data": {
            "method": "tools/call", "name": name, "arguments": {},
        }}],
    }
    return client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": rpc_id, "method": "SendMessage",
              "params": {"message": message}},
        headers={"A2A-Version": "1.0", **(headers or {"Authorization": f"Bearer {_jwt(claims)}"})},
    )


def test_dispatch_is_not_publication_gated_for_authorized_callers(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.example/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    orch = _orchestrator()
    registry = orch.a2a_publications = _registry()
    record = _publish(registry)
    client = _executor_client(orch)
    from orchestrator import mcp_projection

    def _fake_resolve(o, user_id, tool_name, claims):
        return SimpleNamespace(agent_id="web-research-1", skill_id="web_search")

    monkeypatch.setattr(mcp_projection, "resolve_projected_tool", _fake_resolve)
    response = _tool_call(client, name="web_search")
    assert response.status_code == 200
    orch.execute_authorized_tool.assert_awaited_once()
    registry.withdraw(publication_id=record.publication_id, actor=ADMIN)
    response = _tool_call(client, name="web_search", rpc_id="c2")
    assert response.status_code == 200
    assert orch.execute_authorized_tool.await_count == 2


# --- real startup wiring -----------------------------------------------------


@pytest.mark.asyncio
async def test_startup_mounts_a2a_and_publication_routes_when_server_enabled(monkeypatch):
    from orchestrator import session_store
    from orchestrator import orchestrator as module
    from shared.a2a_publication import A2APublicationRegistry
    from tests.test_evidence_entry import lifetime_host

    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.delenv("A2A_EXTERNAL_AGENTS", raising=False)
    monkeypatch.setattr(session_store, "assert_production_posture", lambda: None)
    monkeypatch.setattr(
        module.flags, "is_enabled", lambda name: name == "a2a_server"
    )
    host = lifetime_host(monkeypatch)
    host.voice_services = None
    host.generated_agent_publication_service = SimpleNamespace(
        recover_once=AsyncMock(return_value=SimpleNamespace(degraded_publication_ids=())),
        start=MagicMock(), close=AsyncMock(),
    )

    async def idle(*_args):
        await asyncio.Event().wait()

    def track(coroutine, *, name):
        coroutine.close()

    host._track_startup_background_task = track
    host.explicit_notes = SimpleNamespace(expiry_loop=idle)
    host._jwks_warm_loop = idle
    host._personal_agent_watchdog_task = None
    host._personal_agent_watchdog_loop = idle
    host._start_phi_warm = MagicMock()
    host.lifecycle_manager = SimpleNamespace(reconcile_orphaned_draft_permissions=lambda: 0,
        reconcile_legacy_directory_ownership=lambda: None)
    host._monitor_agents = idle
    host.task_manager = SimpleNamespace(prune_missing=MagicMock())
    host.async_task_manager.start_retention_sweep = MagicMock()
    serving = AsyncMock()
    captured = {}

    def capture(config):
        captured["app"] = config.app
        return SimpleNamespace(serve=serving)

    monkeypatch.setattr(module.uvicorn, "Server", capture)
    await host._run_started_server()
    serving.assert_awaited_once()
    assert isinstance(host.a2a_publications, A2APublicationRegistry)
    route_paths = set()
    for route in captured["app"].router.routes:
        route_paths.add(getattr(route, "path", ""))
        original = getattr(route, "original_router", None)
        for sub in getattr(original, "routes", []) or []:
            route_paths.add(getattr(sub, "path", ""))
    assert "/a2a" in route_paths
    assert "/a2a/.well-known/agent-card.json" in route_paths
    assert "/api/a2a/publications" in route_paths
    assert "/api/a2a/publications/catalog" in route_paths
    await host._close_started_services()


# --- admin REST API ----------------------------------------------------------


def _api_client(monkeypatch, orch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator.a2a_publication_api import publication_router

    app = FastAPI()
    app.include_router(publication_router)
    app.state.orchestrator = orch
    return TestClient(app)


def test_api_requires_authentication(monkeypatch):
    client = _api_client(monkeypatch, _orchestrator())
    for method, url in (("get", "/api/a2a/publications"), ("get", "/api/a2a/publications/catalog")):
        response = getattr(client, method)(url)
        assert response.status_code == 401, url


def test_api_requires_admin_role(monkeypatch):
    client = _api_client(monkeypatch, _orchestrator())
    for method, url in (
        ("get", "/api/a2a/publications"),
        ("post", "/api/a2a/publications/intents"),
        ("delete", "/api/a2a/publications/pub-1"),
    ):
        response = getattr(client, method)(url, headers=_user_headers(admin=False))
        assert response.status_code == 403, url


def test_api_disabled_without_registry_returns_conflict(monkeypatch):
    client = _api_client(monkeypatch, _orchestrator())
    response = client.get("/api/a2a/publications", headers=_admin_headers())
    assert response.status_code == 409
    assert "not enabled" in response.json()["detail"]


def test_api_full_publish_inventory_withdraw_cycle(monkeypatch):
    orch = _orchestrator()
    registry = _registry()
    orch.a2a_publications = registry
    client = _api_client(monkeypatch, orch)

    catalog = client.get("/api/a2a/publications/catalog", headers=_admin_headers()).json()
    capabilities = {c["agent_id"]: c for c in catalog["capabilities"]}
    assert capabilities["weather-1"]["audiences"] == ["tools:read", "tools:search"]
    assert capabilities["weather-1"]["published"] is False
    assert "30d" in catalog["lifetimes"]

    intent_response = client.post(
        "/api/a2a/publications/intents",
        headers=_admin_headers(),
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "7d"},
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()
    assert intent["confirmation_token"]

    confirm = client.post(
        f"/api/a2a/publications/intents/{intent['intent_id']}/confirm",
        headers=_admin_headers(),
        json={"confirmation_token": intent["confirmation_token"]},
    )
    assert confirm.status_code == 200
    publication = confirm.json()
    assert publication["status"] == "active"

    inventory = client.get("/api/a2a/publications", headers=_admin_headers()).json()
    ids = {p["publication_id"] for p in inventory["publications"]}
    assert publication["publication_id"] in ids
    assert intent["confirmation_token"] not in json.dumps(inventory)

    catalog = client.get("/api/a2a/publications/catalog", headers=_admin_headers()).json()
    capabilities = {c["agent_id"]: c for c in catalog["capabilities"]}
    assert capabilities["weather-1"]["published"] is True

    withdrawn = client.delete(
        f"/api/a2a/publications/{publication['publication_id']}",
        headers=_admin_headers(),
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"
    capabilities = {
        c["agent_id"]: c
        for c in client.get(
            "/api/a2a/publications/catalog", headers=_admin_headers(),
        ).json()["capabilities"]
    }
    assert capabilities["weather-1"]["published"] is False


def test_api_intent_rejects_unregistered_audience(monkeypatch):
    orch = _orchestrator()
    orch.a2a_publications = _registry()
    client = _api_client(monkeypatch, orch)
    response = client.post(
        "/api/a2a/publications/intents",
        headers=_admin_headers(),
        json={"agent_id": "weather-1", "audiences": ["tools:system"], "lifetime": "7d"},
    )
    assert response.status_code == 400


def test_api_confirm_with_wrong_token_is_forbidden(monkeypatch):
    orch = _orchestrator()
    orch.a2a_publications = _registry()
    client = _api_client(monkeypatch, orch)
    intent = client.post(
        "/api/a2a/publications/intents",
        headers=_admin_headers(),
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "7d"},
    ).json()
    response = client.post(
        f"/api/a2a/publications/intents/{intent['intent_id']}/confirm",
        headers=_admin_headers(),
        json={"confirmation_token": "bad"},
    )
    assert response.status_code == 403


def test_api_withdraw_unknown_publication_is_not_found(monkeypatch):
    orch = _orchestrator()
    orch.a2a_publications = _registry()
    client = _api_client(monkeypatch, orch)
    response = client.delete("/api/a2a/publications/missing", headers=_admin_headers())
    assert response.status_code == 404


def test_api_withdraw_twice_conflict_and_stale_confirm(monkeypatch):
    orch = _orchestrator()
    registry = _registry()
    orch.a2a_publications = registry
    client = _api_client(monkeypatch, orch)
    headers = _admin_headers()
    publication = client.post(
        "/api/a2a/publications/intents", headers=headers,
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "1h"},
    ).json()
    confirmed = client.post(
        f"/api/a2a/publications/intents/{publication['intent_id']}/confirm",
        headers=headers, json={"confirmation_token": publication["confirmation_token"]},
    ).json()
    assert client.delete(
        f"/api/a2a/publications/{confirmed['publication_id']}", headers=headers,
    ).status_code == 200
    assert client.delete(
        f"/api/a2a/publications/{confirmed['publication_id']}", headers=headers,
    ).status_code == 409

    intent = client.post(
        "/api/a2a/publications/intents", headers=headers,
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "1h"},
    ).json()
    registry.clock.advance(601)
    stale = client.post(
        f"/api/a2a/publications/intents/{intent['intent_id']}/confirm",
        headers=headers, json={"confirmation_token": intent["confirmation_token"]},
    )
    assert stale.status_code == 409


def test_api_orchestrator_unavailable_returns_503(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator.a2a_publication_api import publication_router

    app = FastAPI()
    app.include_router(publication_router)
    client = TestClient(app)
    response = client.get("/api/a2a/publications", headers=_admin_headers())
    assert response.status_code == 503


def test_api_records_audit_events_through_recorder(monkeypatch):
    orch = _orchestrator()
    orch.a2a_publications = _registry()
    client = _api_client(monkeypatch, orch)
    recorded = []

    class _FakeRecorder:
        async def record(self, event):
            recorded.append(event)
            return None

    from audit import recorder as audit_recorder

    monkeypatch.setattr(audit_recorder, "get_recorder", lambda: _FakeRecorder())
    headers = _admin_headers()
    client.post(
        "/api/a2a/publications/intents", headers=headers,
        json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "7d"},
    )
    assert any("intent_created" in event.action_type for event in recorded)


def test_registry_audit_sink_records_through_recorder_and_tolerates_failures(monkeypatch):
    from orchestrator.a2a_orchestrator_executor import attach_publication_registry

    recorded = []

    class _FakeRecorder:
        def __init__(self, fail=False):
            self.fail = fail

        def record_blocking(self, event):
            if self.fail:
                raise RuntimeError("audit unavailable")
            recorded.append(event)

    from audit import recorder as audit_recorder

    monkeypatch.setattr(audit_recorder, "get_recorder", lambda: _FakeRecorder())
    orch = _orchestrator()
    orch.tool_permissions._safe.add("web-research-1")
    registry = attach_publication_registry(orch)
    _publish(registry)
    assert any("a2a_publication.published" in event.action_type for event in recorded)

    monkeypatch.setattr(audit_recorder, "get_recorder", lambda: _FakeRecorder(fail=True))
    _publish(registry, agent_id="web-research-1", audiences=["tools:search"])
    assert registry.is_published("web-research-1")


# --- admin tools chrome surface ---------------------------------------------


def _surface_roles(admin=True):
    return ["admin"] if admin else ["user"]


@pytest.fixture
def a2a_flag(monkeypatch):
    from shared.feature_flags import flags

    def _set(value: bool):
        monkeypatch.setitem(flags._flags, "a2a_server", value)

    return _set


def test_surface_render_requires_admin():
    from orchestrator.projection_surfaces import admin_tools

    import asyncio

    html = asyncio.new_event_loop().run_until_complete(
        admin_tools.render(_orchestrator(), "u", _surface_roles(False), {"tab": "a2a"})
    )
    assert "Admin role required" in html


def test_surface_render_shows_disabled_notice_without_flag(a2a_flag):
    from orchestrator.projection_surfaces import admin_tools

    a2a_flag(False)
    orch = _orchestrator()
    import asyncio

    html = asyncio.new_event_loop().run_until_complete(
        admin_tools.render(orch, ADMIN, _surface_roles(), {"tab": "a2a"})
    )
    assert "disabled" in html


def test_surface_render_shows_rest_guidance_and_inventory(a2a_flag):
    from orchestrator.projection_surfaces import admin_tools

    a2a_flag(True)
    orch = _orchestrator()
    registry = _registry()
    orch.a2a_publications = registry
    record = _publish(registry)
    registry.withdraw(publication_id=record.publication_id, actor=ADMIN)
    import asyncio

    html = asyncio.new_event_loop().run_until_complete(
        admin_tools.render(orch, ADMIN, _surface_roles(), {"tab": "a2a"})
    )
    assert "A2A publishing" in html
    assert "Weather" in html
    assert "weather-1" in html
    assert "draft-secret-9" not in html
    assert "POST /api/a2a/publications/intents" in html
    assert record.publication_id in html
    assert "tools:read" in html
    assert "withdrawn" in html
    assert "chrome_a2a" not in html
    assert not registry.pending_intents()


def test_surface_render_flag_off_hides_capabilities(a2a_flag):
    from orchestrator.projection_surfaces import admin_tools

    a2a_flag(False)
    orch = _orchestrator()
    orch.a2a_publications = _registry()
    import asyncio

    html = asyncio.new_event_loop().run_until_complete(
        admin_tools.render(orch, ADMIN, _surface_roles(), {"tab": "a2a"})
    )
    assert "Weather" not in html


def test_surface_native_components_admin_and_other_tabs(a2a_flag):
    from orchestrator.projection_surfaces import admin_tools

    a2a_flag(True)
    orch = _orchestrator()
    registry = _registry()
    orch.a2a_publications = registry
    import asyncio

    loop = asyncio.new_event_loop()
    denied = loop.run_until_complete(
        admin_tools.components(orch, "u", _surface_roles(False), {"tab": "a2a"})
    )
    assert "Admin role required" in denied[0]["message"]
    other = loop.run_until_complete(
        admin_tools.components(orch, ADMIN, _surface_roles(), {"tab": "quality"})
    )
    assert other
    comps = loop.run_until_complete(
        admin_tools.components(orch, ADMIN, _surface_roles(), {"tab": "a2a"})
    )
    blob = json.dumps(comps)
    assert "weather-1" in blob
    assert "draft-secret-9" not in blob
    assert "chrome_a2a" not in blob
    assert "/api/a2a/publications" in blob


def test_surface_title_reports_a2a_tab():
    from orchestrator.projection_surfaces import admin_tools

    import asyncio

    title = asyncio.new_event_loop().run_until_complete(
        admin_tools.title(None, ADMIN, {"tab": "a2a"})
    )
    assert title == "A2A publishing"


def test_surface_render_empty_catalog_and_error_branches(a2a_flag):
    from orchestrator.projection_surfaces import admin_tools
    from shared.a2a_publication import A2APublicationRegistry

    a2a_flag(True)
    orch = _orchestrator()
    orch.a2a_publications = A2APublicationRegistry(
        catalog=lambda: (), audiences=lambda agent_id: (),
        time_fn=_Clock(), id_factory=lambda: "x", token_factory=lambda: "t",
    )
    import asyncio

    html = asyncio.new_event_loop().run_until_complete(
        admin_tools.render(orch, ADMIN, _surface_roles(), {"tab": "a2a"})
    )
    assert "No approved capabilities" in html
    assert "No staged publications" in html
    assert "Nothing published for A2A" in html

    def _boom():
        raise RuntimeError("catalog unavailable")

    orch.a2a_publications._catalog = _boom
    failed = asyncio.new_event_loop().run_until_complete(
        admin_tools.render(orch, ADMIN, _surface_roles(), {"tab": "a2a"})
    )
    assert "Failed to load A2A publication state" in failed


def test_surface_render_denies_non_admin_directly_and_disabled_registry(a2a_flag):
    from orchestrator.projection_surfaces import admin_tools

    a2a_flag(True)
    denied = admin_tools._render_a2a(_orchestrator(), _surface_roles(False), {})
    assert "Admin role required" in denied
    a2a_flag(False)
    disabled = admin_tools._render_a2a(_orchestrator(), _surface_roles(True), {})
    assert "disabled" in disabled


def test_surface_render_no_audience_and_published_rows(a2a_flag):
    from orchestrator.projection_surfaces import admin_tools

    a2a_flag(True)
    orch = _orchestrator()
    registry2 = _registry(audiences={"weather-1": ("tools:read",), "web-research-1": ()})
    orch.a2a_publications = registry2
    _publish(registry2)
    import asyncio

    html = asyncio.new_event_loop().run_until_complete(
        admin_tools.render(orch, ADMIN, _surface_roles(), {"tab": "a2a"})
    )
    assert "no audiences to publish" in html
    assert "data-a2a-publication" in html


def test_surface_components_disabled_and_published_branches(a2a_flag):
    from orchestrator.projection_surfaces import admin_tools

    a2a_flag(False)
    orch = _orchestrator()
    orch.a2a_publications = _registry()
    import asyncio

    loop = asyncio.new_event_loop()
    disabled = loop.run_until_complete(
        admin_tools.components(orch, ADMIN, _surface_roles(), {"tab": "a2a"})
    )
    assert "disabled" in disabled[0]["message"]
    a2a_flag(True)
    registry2 = _registry(audiences={"weather-1": ("tools:read",), "web-research-1": ()})
    orch.a2a_publications = registry2
    _publish(registry2)
    comps = loop.run_until_complete(
        admin_tools.components(orch, ADMIN, _surface_roles(), {"tab": "a2a"})
    )
    blob = json.dumps(comps)
    assert "published" in blob
    assert "no audiences to publish" in blob
