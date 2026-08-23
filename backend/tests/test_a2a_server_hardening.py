"""Hardening of the orchestrator's OWN inbound A2A server (FF_A2A_SERVER).

Four audit findings, each pinned here:

1. Cross-caller task disclosure — the SDK's default call context is an
   ``UnauthenticatedUser`` and ``InMemoryTaskStore`` keys tasks by that owner,
   while ``tasks/list``/``tasks/get``/``tasks/cancel`` are served BEFORE the
   executor's bearer check. Now: bearer gate before dispatch, tasks scoped to
   the verified subject, anonymous scope empty.
2. Anonymous enumeration — the public card and an empty ``message/send``
   listed EVERY registered agent's tools. Now: the card names only public,
   owner-safe built-ins; listing requires a bearer and is projected per user
   through the same predicate chat and /mcp use.
3. Weak token validation — no issuer check, ``azp`` optional,
   ``KEYCLOAK_ALLOWED_AZP`` ignored, delegation tokens accepted, JWKS cached
   forever. Now: at least as strict as the web entry gate.
4. Stale posture comments (checked by reading them, not here).
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

from shared.protocol import MCPResponse  # noqa: E402

ALICE = "alice-sub"
BOB = "bob-sub"


# ----------------------------------------------------------------- helpers


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


def _user_claims(sub: str, **extra) -> dict:
    claims = {
        "sub": sub,
        "preferred_username": sub,
        "iss": "https://iam.example/realms/astral",
        "azp": "astral-frontend",
        "aud": ["astral-agent-service", "account"],
        "realm_access": {"roles": ["user"]},
    }
    claims.update(extra)
    return claims


def _auth(sub: str, **extra) -> dict:
    return {"Authorization": f"Bearer {_jwt(_user_claims(sub, **extra))}"}


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
    """Per-user tool permissions: Bob may not use the weather tool."""

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


def _orchestrator():
    """Three agents: a safe public built-in, an unsafe built-in, a private draft."""
    orch = SimpleNamespace()
    orch.agent_cards = {
        "weather-1": _card("weather-1", "Weather", [_skill("get_weather")]),
        "web-research-1": _card("web-research-1", "Web Research", [_skill("web_search", "tools:search")]),
        "draft-secret-9": _card("draft-secret-9", "Secret Draft", [_skill("exfiltrate", "tools:write")]),
    }
    orch.agents = {}
    orch.local_agents = {"weather-1": object(), "web-research-1": object(), "draft-secret-9": object()}
    orch.security_flags = {}
    orch._is_draft_agent = lambda agent_id: False
    orch.tool_permissions = _Permissions(safe_ids={"weather-1"})
    orch.execute_authorized_tool = AsyncMock(
        return_value=MCPResponse(request_id="r1", result={"temp": 21})
    )
    return orch


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.example/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_ALLOWED_AZP", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


@pytest.fixture
def client(mock_env):
    from orchestrator.a2a_orchestrator_executor import setup_orchestrator_a2a

    orch = _orchestrator()
    app = FastAPI()
    setup_orchestrator_a2a(app, orch)
    tc = TestClient(app)
    tc.orch = orch  # type: ignore[attr-defined]
    return tc


V1 = {"A2A-Version": "1.0"}


def _send(client, headers, parts, rpc_id="1"):
    msg = {"message_id": f"m-{rpc_id}", "role": "ROLE_USER", "parts": parts}
    return client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": rpc_id, "method": "SendMessage",
              "params": {"message": msg}},
        headers={**V1, **headers},
    )


def _rpc(client, headers, method, params, rpc_id="9"):
    version = {} if "/" in method else V1
    return client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
        headers={**version, **headers},
    )


def _task(body: dict) -> dict:
    """v1.0 SendMessage wraps the Task as ``result.task``."""
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


# ----------------------------------------------------- (2) anonymous card


def test_public_card_names_only_safe_public_builtins(client):
    resp = client.get("/a2a/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    ids = {s["id"] for s in card["skills"]}
    assert "weather-1" in ids                 # public + safe + connected
    assert "web-research-1" not in ids        # public but NOT safe-marked
    assert "draft-secret-9" not in ids        # not a public built-in
    # Generic per-agent skills, never the per-tool inventory.
    assert not ids & {"get_weather", "web_search", "exfiltrate"}
    assert "chat" in ids
    blob = json.dumps(card)
    assert "exfiltrate" not in blob
    assert "draft-secret-9" not in blob


def test_public_card_prefers_public_base_url(mock_env, monkeypatch):
    from orchestrator.a2a_orchestrator_executor import build_orchestrator_a2a_card

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://sandbox.example/")
    card = build_orchestrator_a2a_card(_orchestrator())
    assert card.supported_interfaces[0].url == "https://sandbox.example/a2a"


# -------------------------------------------------- (1) bearer before dispatch


@pytest.mark.parametrize("method,params", [
    ("message/send", {"message": {"message_id": "m", "role": "ROLE_USER", "parts": [{"text": ""}]}}),
    ("tasks/list", {}),
    ("tasks/get", {"id": "t-1"}),
    ("tasks/cancel", {"id": "t-1"}),
    ("ListTasks", {}),
    ("GetTask", {"id": "t-1"}),
    ("CancelTask", {"id": "t-1"}),
])
def test_anonymous_rpc_is_refused_before_dispatch(client, method, params):
    resp = _rpc(client, {}, method, params)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")
    assert resp.json()["error"]["message"] == "Authentication required"
    client.orch.execute_authorized_tool.assert_not_awaited()


def test_token_without_entry_role_is_refused(client):
    headers = {"Authorization": f"Bearer {_jwt(_user_claims(ALICE, realm_access={'roles': ['viewer']}))}"}
    assert _rpc(client, headers, "tasks/list", {}).status_code == 401


def test_delegation_token_is_refused_inbound(client):
    headers = {"Authorization": f"Bearer {_jwt(_user_claims(ALICE, act={'sub': 'agent:weather-1'}))}"}
    assert _rpc(client, headers, "tasks/list", {}).status_code == 401
    headers = {"Authorization": f"Bearer {_jwt(_user_claims(ALICE, delegation=True))}"}
    assert _rpc(client, headers, "tasks/list", {}).status_code == 401


def test_undecodable_mock_token_does_not_become_test_user(client):
    """The mock fallback to a permissive default principal is closed at the gate."""
    assert _rpc(client, {"Authorization": "Bearer garbage"}, "tasks/list", {}).status_code == 401


def test_caller_cannot_see_or_cancel_another_callers_task(client):
    # Alice creates a task (an authenticated discovery turn).
    created = _send(client, _auth(ALICE), [{"text": ""}], rpc_id="a1")
    assert created.status_code == 200, created.text
    task_id = _task(created.json())["id"]

    # Alice sees her own task.
    mine = _rpc(client, _auth(ALICE), "GetTask", {"id": task_id})
    assert mine.status_code == 200 and mine.json()["result"]["id"] == task_id
    listed = _rpc(client, _auth(ALICE), "ListTasks", {})
    assert task_id in {t["id"] for t in listed.json()["result"].get("tasks", [])}

    # Bob — valid principal, different subject — sees nothing of it.
    bob_get = _rpc(client, _auth(BOB), "GetTask", {"id": task_id})
    assert "error" in bob_get.json(), bob_get.text
    bob_list = _rpc(client, _auth(BOB), "ListTasks", {})
    assert bob_list.json()["result"].get("tasks", []) == []
    bob_cancel = _rpc(client, _auth(BOB), "CancelTask", {"id": task_id})
    assert "error" in bob_cancel.json(), bob_cancel.text

    # Anonymous sees nothing at all (v1.0 and v0.3 method names alike).
    for method in ("GetTask", "tasks/get"):
        assert _rpc(client, {}, method, {"id": task_id}).status_code == 401
    for method in ("ListTasks", "tasks/list"):
        assert _rpc(client, {}, method, {}).status_code == 401


def test_anonymous_task_scope_is_empty_and_unshared():
    """Store-level backstop: an unauthenticated context never lands in a shared bucket."""
    from a2a.auth.user import UnauthenticatedUser
    from a2a.server.context import ServerCallContext
    from orchestrator.a2a_orchestrator_executor import A2APrincipal, a2a_task_owner

    anon_a = a2a_task_owner(ServerCallContext(user=UnauthenticatedUser()))
    anon_b = a2a_task_owner(ServerCallContext(user=UnauthenticatedUser()))
    assert anon_a != anon_b and anon_a != "" and anon_b != ""
    principal = A2APrincipal({"sub": ALICE}, "tok")
    assert a2a_task_owner(ServerCallContext(user=principal)) == f"sub:{ALICE}"
    assert a2a_task_owner(ServerCallContext(user=principal)) == a2a_task_owner(
        ServerCallContext(user=A2APrincipal({"sub": ALICE}, "other-tok"))
    )


# ------------------------------------------ (2) per-user projected listing


def test_tools_list_data_part_returns_callers_projected_tools(client):
    alice = _send(client, _auth(ALICE), [{"data": {"method": "tools/list"}}], rpc_id="l1")
    assert alice.status_code == 200, alice.text
    assert _tool_names(alice.json()) == {"get_weather", "web_search", "exfiltrate"}

    bob = _send(client, _auth(BOB), [{"data": {"method": "tools/list"}}], rpc_id="l2")
    assert _tool_names(bob.json()) == {"web_search", "exfiltrate"}


def test_empty_and_text_messages_list_per_user(client):
    empty = _send(client, _auth(BOB), [{"text": ""}], rpc_id="e1")
    assert _tool_names(empty.json()) == {"web_search", "exfiltrate"}
    text = _send(client, _auth(BOB), [{"text": "what's the weather"}], rpc_id="e2")
    assert _tool_names(text.json()) == {"web_search", "exfiltrate"}


def test_listing_mirrors_chat_visibility_gates(client):
    """A draft that chat hides is hidden here too (same predicate)."""
    client.orch._is_draft_agent = lambda agent_id: agent_id == "draft-secret-9"
    resp = _send(client, _auth(ALICE), [{"data": {"method": "tools/list"}}], rpc_id="v1")
    assert "exfiltrate" not in _tool_names(resp.json())


# ------------------------------------------------ tools/call through gates


def test_tools_call_resolves_through_projection_and_authorized_dispatch(client):
    resp = _send(
        client, _auth(ALICE),
        [{"data": {"method": "tools/call", "name": "get_weather", "arguments": {"city": "Lexington"}}}],
        rpc_id="c1",
    )
    assert resp.status_code == 200, resp.text
    assert _data_part(resp.json()) == {"temp": 21}
    kwargs = client.orch.execute_authorized_tool.await_args.kwargs
    assert kwargs["user_id"] == ALICE
    assert kwargs["agent_id"] == "weather-1"
    assert kwargs["tool_name"] == "get_weather"
    assert kwargs["channel"] == "a2a"
    assert kwargs["delegation_subject_token"] == _auth(ALICE)["Authorization"].split(" ", 1)[1]


def test_tools_call_hidden_tool_is_non_disclosing_and_never_dispatched(client):
    resp = _send(
        client, _auth(BOB),
        [{"data": {"method": "tools/call", "name": "get_weather", "arguments": {}}}],
        rpc_id="c2",
    )
    assert resp.status_code == 200
    assert _task(resp.json())["status"]["state"] == "TASK_STATE_FAILED"
    texts = [p.get("text", "") for p in _result_parts(resp.json())]
    assert any("unavailable or not authorized" in t for t in texts)
    client.orch.execute_authorized_tool.assert_not_awaited()

    unknown = _send(
        client, _auth(ALICE),
        [{"data": {"method": "tools/call", "name": "no_such_tool", "arguments": {}}}],
        rpc_id="c3",
    )
    assert _task(unknown.json())["status"]["state"] == "TASK_STATE_FAILED"
    client.orch.execute_authorized_tool.assert_not_awaited()


# ------------------------------------------- (3) Keycloak token validation


@pytest.fixture
def keycloak_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.example/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-desktop")
    monkeypatch.setenv("AGENT_SERVICE_CLIENT_ID", "astral-agent-service")


def _validator(monkeypatch, claims, *, strict=True):
    from shared import a2a_security

    validator = a2a_security.A2ASecurityValidator(require_first_party_user=strict)
    validator._get_jwks = AsyncMock(return_value={"keys": []})
    monkeypatch.setattr(
        a2a_security.jose_jwt, "decode", lambda token, key, **kw: dict(claims)
    )
    return validator


@pytest.mark.asyncio
async def test_strict_accepts_first_party_user_token(keycloak_env, monkeypatch):
    v = _validator(monkeypatch, _user_claims(ALICE))
    assert (await v.validate_token("t"))["sub"] == ALICE


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    {"iss": "https://other.example/realms/astral"},
    {"iss": None},
    {"azp": None},
    {"azp": "evil-client"},
    {"act": {"sub": "agent:weather-1"}},
    {"delegation": True},
    {"aud": "astral-mcp"},
    {"aud": ["some-other-api"]},
    {"realm_access": {"roles": ["viewer"]}},
    {"realm_access": None, "resource_access": None},
])
async def test_strict_refuses_weak_tokens(keycloak_env, monkeypatch, mutation):
    claims = _user_claims(ALICE)
    for key, value in mutation.items():
        if value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    v = _validator(monkeypatch, claims)
    assert await v.validate_token("t") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    {"azp": "astral-desktop"},            # KEYCLOAK_ALLOWED_AZP honored
    {"azp": "astral-agent-service"},      # agent-service client
    {"aud": "account"},                   # Keycloak default audience
    {"aud": "astral-frontend"},           # our client id
    {"aud": None},                        # no audience → azp decides
    {"iss": "https://iam.example/realms/astral/"},  # trailing slash tolerated
    {"realm_access": {"roles": []}, "resource_access": {"astral-frontend": {"roles": ["admin"]}}},
])
async def test_strict_accepts_web_gate_equivalents(keycloak_env, monkeypatch, mutation):
    claims = _user_claims(ALICE)
    for key, value in mutation.items():
        if value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    v = _validator(monkeypatch, claims)
    assert (await v.validate_token("t"))["sub"] == ALICE


@pytest.mark.asyncio
async def test_agent_posture_still_accepts_delegation_tokens(keycloak_env, monkeypatch):
    """An AGENT's /a2a is dialled by the orchestrator with an RFC 8693 token."""
    claims = _user_claims(ALICE, act={"sub": "agent:weather-1"}, delegation=True)
    claims.pop("realm_access")
    v = _validator(monkeypatch, claims, strict=False)
    assert (await v.validate_token("t"))["sub"] == ALICE
    # ...but never one from another realm, and KEYCLOAK_ALLOWED_AZP is honored.
    assert await _validator(
        monkeypatch, _user_claims(ALICE, iss="https://other.example"), strict=False
    ).validate_token("t") is None
    assert await _validator(
        monkeypatch, _user_claims(ALICE, azp="evil-client"), strict=False
    ).validate_token("t") is None
    assert (await _validator(
        monkeypatch, _user_claims(ALICE, azp="astral-desktop"), strict=False
    ).validate_token("t"))["sub"] == ALICE


@pytest.mark.asyncio
async def test_jwks_uses_shared_ttl_cache_with_kid_miss_refetch(keycloak_env, monkeypatch):
    from shared import a2a_security, jwks_cache

    jwks_cache.clear()
    fetches = []

    async def fake_fetch(url):
        fetches.append(url)
        jwks = {"keys": [{"kid": "k1"}]}
        jwks_cache._cache[url] = {"jwks": jwks, "fetched_at": jwks_cache.time.time()}
        return jwks

    monkeypatch.setattr(jwks_cache, "_fetch", fake_fetch)
    v = a2a_security.A2ASecurityValidator(require_first_party_user=True)

    def token_with_kid(kid):
        header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": kid}).encode()).decode().rstrip("=")
        return f"{header}.e30.sig"

    await v._get_jwks(token_with_kid("k1"))
    await v._get_jwks(token_with_kid("k1"))
    assert len(fetches) == 1                      # cached, not refetched per call
    await v._get_jwks(token_with_kid("k2"))
    assert len(fetches) == 2                      # kid miss → refetch (rotation)
    jwks_cache._cache[fetches[0]]["fetched_at"] -= jwks_cache._TTL_SECONDS + 1
    await v._get_jwks(token_with_kid("k1"))
    assert len(fetches) == 3                      # TTL expiry → refetch
    jwks_cache.clear()


@pytest.mark.asyncio
async def test_unconfigured_keycloak_refuses(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.delenv("KEYCLOAK_AUTHORITY", raising=False)
    monkeypatch.delenv("KEYCLOAK_CLIENT_ID", raising=False)
    from shared.a2a_security import A2ASecurityValidator

    assert await A2ASecurityValidator(require_first_party_user=True).validate_token("t") is None


# --------------------------------------------------------------- flag off


def test_flag_off_mounts_nothing(monkeypatch):
    """Flag-off stays byte-identical: no /a2a route exists unless enabled."""
    from shared.feature_flags import FeatureFlags

    monkeypatch.delenv("FF_A2A_SERVER", raising=False)
    assert FeatureFlags().is_enabled("a2a_server") is False
    app = FastAPI()
    paths = {getattr(r, "path", None) for r in app.router.routes}
    assert "/a2a" not in paths and "/a2a/.well-known/agent-card.json" not in paths
