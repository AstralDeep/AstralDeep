"""Covers consent-preserving publication capacity and event-loop progress through real REST and A2A routes.
The registry and handlers remain real while barriers isolate blocking storage and ownership boundaries.
"""

import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI

from orchestrator.a2a_orchestrator_executor import setup_orchestrator_a2a
from orchestrator.a2a_publication_api import publication_router
from shared.a2a_publication import A2APublicationError, A2APublicationRegistry
from tests.test_a2a_publication import (
    ALICE, ALICE_EMAIL, _Clock, _Store, _api_client, _auth, _orchestrator, _task,
    mock_env as mock_env,
)


def _publish_owned(registry, agent_id="weather-1", actor=ALICE, lifetime="open"):
    intent = registry.create_intent(agent_id=agent_id, audiences=["tools:read"],
                                    lifetime=lifetime, actor=actor, owner=actor)
    return registry.confirm(intent_id=intent.intent_id,
                            confirmation_token=intent.confirmation_token, actor=actor)


def _bounded_registry(*, store=None, clock=None, events=None):
    return A2APublicationRegistry(catalog=lambda: ("weather-1", "web-research-1", "third"),
        audiences=lambda _: ("tools:read",), max_inventory=2, store=store,
        time_fn=clock or _Clock(), audit_sink=None if events is None else events.append)


@pytest.mark.parametrize("inactive", ["withdrawn", "expired"])
def test_history_trimming_preserves_other_owner_open_consent(inactive):
    clock = _Clock()
    store = _Store()
    first = _bounded_registry(store=store, clock=clock)
    original = _publish_owned(first, actor="alice")
    historical = _publish_owned(first, "web-research-1", "bob", "1h")
    if inactive == "withdrawn":
        first.withdraw(publication_id=historical.publication_id, actor="bob")
    else:
        clock.advance(3601)
    replacement = _publish_owned(first, "web-research-1", "bob")
    restarted = _bounded_registry(store=store, clock=clock)
    assert restarted.get(original.publication_id) == original
    assert restarted.active_agent_ids() == ("weather-1", "web-research-1")
    assert restarted.get(historical.publication_id) is None
    assert restarted.inventory(actor="alice")[0]["publication_id"] == original.publication_id
    assert restarted.inventory(actor="bob")[0]["publication_id"] == replacement.publication_id
    assert len(store.state["publications"]) == 2


def test_all_active_capacity_consumes_denied_intent_without_evicting_consent():
    events = []
    registry = _bounded_registry(events=events)
    original = _publish_owned(registry, actor="alice")
    second = _publish_owned(registry, "web-research-1", "bob")
    intent = registry.create_intent(agent_id="third", audiences=["tools:read"],
                                    lifetime="open", actor="carol", owner="carol")
    with pytest.raises(A2APublicationError, match="capacity"):
        registry.confirm(intent_id=intent.intent_id,
                         confirmation_token=intent.confirmation_token, actor="carol")
    assert registry.get_intent(intent.intent_id) is None
    assert registry.active() == (original, second)
    assert [event["action"] for event in events].count("published") == 2
    assert events[-1]["action"] == "confirm_denied"
    registry.withdraw(publication_id=second.publication_id, actor="bob")
    replacement = _publish_owned(registry, "third", "carol")
    assert registry.active() == (original, replacement)


def test_capacity_refusal_is_http_conflict_and_keeps_existing_publication(mock_env):
    orch = _orchestrator(safe=("weather-1", "web-research-1"),
                         owners={"weather-1": ALICE_EMAIL, "web-research-1": ALICE_EMAIL})
    orch.a2a_publications._max_inventory = 1
    client = _api_client(orch)
    headers = _auth()
    def propose(agent_id, scope):
        return client.post("/api/a2a/publications/intents", headers=headers,
                           json={"agent_id": agent_id, "audiences": [scope], "lifetime": "open"}).json()
    first = propose("weather-1", "tools:read")
    response = client.post(f"/api/a2a/publications/intents/{first['intent_id']}/confirm",
        headers=headers, json={"confirmation_token": first["confirmation_token"]})
    assert response.status_code == 200
    first_id = response.json()["publication_id"]
    second = propose("web-research-1", "tools:search")
    response = client.post(f"/api/a2a/publications/intents/{second['intent_id']}/confirm",
        headers=headers, json={"confirmation_token": second["confirmation_token"]})
    assert response.status_code == 409
    assert orch.a2a_publications.get(first_id).status == "active"
    assert orch.a2a_publications.get_intent(second["intent_id"]) is None


class _ReadBarrier:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.on_loop = None

    def block(self):
        if self.on_loop is not None:
            return
        self.on_loop = threading.get_ident() == self.loop_thread
        self.loop.call_soon_threadsafe(self.entered.set)
        if not self.on_loop and not self.release.wait(timeout=10):
            raise RuntimeError("publication barrier was not released")

    async def run(self, operation):
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(self.entered.wait(), timeout=10)
            progressed_while_blocked = not task.done()
        finally:
            self.release.set()
        result = await task
        assert self.on_loop is False
        assert progressed_while_blocked
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("route,boundary", [
    ("inventory", "load"), ("catalog", "load"), ("create", "load"),
    ("confirm", "load"), ("withdraw", "load"),
    ("catalog", "owner"), ("create", "owner"), ("confirm", "owner"),
    ("withdraw", "owner"), ("list", "load"), ("list", "owner"),
    ("call", "load"), ("call", "owner"), ("deny", "save"),
    ("card", "load"), ("card", "owner"),
])
async def test_publication_requests_allow_loop_progress_during_blocking_read(mock_env, route, boundary):
    orch = _orchestrator(owners={"weather-1": ALICE_EMAIL})
    store = _Store()
    registry = orch.a2a_publications
    registry._store = store
    intent = registry.create_intent(agent_id="weather-1", audiences=["tools:read"],
        lifetime="open", actor=ALICE, owner=ALICE_EMAIL)
    record = None
    if route not in {"confirm", "deny"}:
        record = registry.confirm(intent_id=intent.intent_id,
            confirmation_token=intent.confirmation_token, actor=ALICE)
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(publication_router)
    setup_orchestrator_a2a(app, orch)
    barrier = _ReadBarrier()
    if boundary == "owner":
        def owner(_):
            barrier.block()
            return ALICE_EMAIL
        orch.a2a_publication_policy._owner_lookup = owner
    else:
        original = getattr(store, boundary)
        def blocked(*args, **kwargs):
            barrier.block()
            return original(*args, **kwargs)
        setattr(store, boundary, blocked)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = _auth()
        if route == "inventory":
            request = client.get("/api/a2a/publications", headers=headers)
        elif route == "catalog":
            request = client.get("/api/a2a/publications/catalog", headers=headers)
        elif route == "create":
            request = client.post("/api/a2a/publications/intents", headers=headers,
                json={"agent_id": "weather-1", "audiences": ["tools:read"], "lifetime": "open"})
        elif route == "confirm":
            request = client.post(f"/api/a2a/publications/intents/{intent.intent_id}/confirm",
                headers=headers, json={"confirmation_token": intent.confirmation_token})
        elif route == "withdraw":
            request = client.delete(f"/api/a2a/publications/{record.publication_id}", headers=headers)
        elif route == "card":
            request = client.get("/a2a/.well-known/agent-card.json")
        else:
            data = {"method": "tools/list"} if route == "list" else {
                "method": "tools/call", "name": "get_weather", "arguments": {}}
            request = client.post("/a2a", headers={**headers, "A2A-Version": "1.0"}, json={
                "jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": {
                    "message_id": "m-1", "role": "ROLE_USER", "parts": [{"data": data}]}}})
        response = await barrier.run(request)
    assert response.status_code == 200
    if route in {"call", "deny"}:
        expected = "TASK_STATE_FAILED" if route == "deny" else "TASK_STATE_COMPLETED"
        assert _task(response.json())["status"]["state"] == expected
