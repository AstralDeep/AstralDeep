"""Feature 028 FR-016 — agent API-key enforcement at registration.

Three layers, per specs/028-workspace-auth-revival:

1. Protocol: ``RegisterAgent`` gained an additive optional ``api_key`` field
   (default ``None``) that survives a to_json/from_json round-trip, and
   pre-028 payloads WITHOUT the field still parse.
2. Orchestrator: the real, unbound ``Orchestrator.register_agent`` bound onto
   a fake ``self`` refuses keyless/wrong-key registrations BEFORE any
   registration state mutates and closes the agent socket with code 1008
   (fail closed — unset ``ASTRAL_ENV`` means production); explicit dev mode
   stays keyless, and a matching ``AGENT_API_KEY`` registers normally.
3. Source: ``shared/base_agent.py`` presents ``os.getenv("AGENT_API_KEY")``
   in the ``RegisterAgent`` it sends, so first-party agents participate in
   the handshake without per-agent code changes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.orchestrator import Orchestrator
from shared.protocol import AgentCard, AgentSkill, RegisterAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeAgentWS:
    """Hashable websocket stand-in recording ``close`` calls."""

    def __init__(self):
        self.close_calls = []

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))


class _FakeUserAgentRegistry:
    """Typed ownership seam used by ``Orchestrator.register_agent``."""

    def __init__(self, default_ownership=None):
        self.default_ownership = default_ownership
        self.ownership = {}
        self.set_calls = []

    def get_agent_ownership(self, agent_id):
        return self.ownership.get(agent_id, self.default_ownership)

    def set_agent_ownership(self, agent_id, owner_email, is_public=False):
        self.set_calls.append((agent_id, owner_email, is_public))
        record = {
            "agent_id": agent_id,
            "owner_email": owner_email,
            "is_public": is_public,
        }
        self.ownership[agent_id] = record
        return record


def _make_card(agent_id: str = "fr016-agent") -> AgentCard:
    return AgentCard(
        name="FR016 Agent",
        description="agent used by the key-enforcement tests",
        agent_id=agent_id,
        skills=[
            AgentSkill(
                name="ping",
                description="echo tool",
                id="ping",
                input_schema={"type": "object", "properties": {}},
                scope="tools:read",
            )
        ],
    )


def _make_fake():
    """Fake orchestrator ``self`` carrying ONLY what ``register_agent``
    touches, with the real implementation bound onto it (house pattern from
    test_component_action.py)."""
    calls = {"register_tool_scopes": [], "cleanup_stale": [], "hook_emits": []}

    async def _safe_send(ws, payload):  # only reached if ui_clients non-empty
        raise AssertionError("no UI clients registered; _safe_send unexpected")

    async def _emit(ctx):
        calls["hook_emits"].append(ctx)

    fake = types.SimpleNamespace(
        agents={},
        agent_cards={},
        agent_capabilities={},
        security_flags={},
        _streamable_tools={},
        ui_clients=[],
        tool_permissions=types.SimpleNamespace(
            register_tool_scopes=lambda aid, scope_map:
                calls["register_tool_scopes"].append((aid, scope_map)),
            cleanup_stale_tool_overrides=lambda aid, names:
                calls["cleanup_stale"].append((aid, names)),
        ),
        security_analyzer=types.SimpleNamespace(analyze_agent=lambda card: {}),
        credential_manager=types.SimpleNamespace(
            register_agent_public_key=lambda *a, **kw: None),
        # Truthy ownership skips the DEFAULT_AGENT_OWNER auto-assign path.
        user_agent_registry=_FakeUserAgentRegistry(
            default_ownership={
                "owner_email": "owner@example.com",
                "is_public": False,
            }
        ),
        hooks=types.SimpleNamespace(emit=_emit),
        _is_draft_agent=lambda aid: False,
        _get_user_id=lambda ws: "fr016-test-user",
        _safe_send=_safe_send,
    )
    fake.register_agent = types.MethodType(Orchestrator.register_agent, fake)
    fake._calls = calls
    return fake


def _assert_no_registration_state(fake):
    """The refusal must land BEFORE any registration state mutates."""
    assert fake.agents == {}
    assert fake.agent_cards == {}
    assert fake.agent_capabilities == {}
    assert fake.security_flags == {}
    assert fake._streamable_tools == {}
    assert fake._calls["register_tool_scopes"] == []
    assert fake._calls["cleanup_stale"] == []


def _assert_registered(fake, ws, card):
    assert fake.agents.get(card.agent_id) is ws
    assert fake.agent_cards.get(card.agent_id) is card
    caps = fake.agent_capabilities.get(card.agent_id)
    assert caps and caps[0]["name"] == "ping"
    assert fake._calls["register_tool_scopes"] == [
        (card.agent_id, {"ping": "tools:read"})]
    assert fake.security_flags.get(card.agent_id) == {}
    assert ws.close_calls == []


# ---------------------------------------------------------------------------
# 1. Protocol — additive api_key field
# ---------------------------------------------------------------------------

def test_register_agent_api_key_defaults_to_none():
    """028 FR-016: api_key is additive-optional — absent unless presented."""
    msg = RegisterAgent(agent_card=_make_card())
    assert msg.api_key is None
    assert json.loads(msg.to_json())["api_key"] is None


def test_register_agent_json_round_trip_preserves_api_key():
    """028 FR-016: the presented key survives the wire round-trip intact,
    alongside the full agent card."""
    msg = RegisterAgent(agent_card=_make_card("fr016-rt"), api_key="secret123")
    parsed = RegisterAgent.from_json(msg.to_json())
    assert parsed.type == "register_agent"
    assert parsed.api_key == "secret123"
    assert parsed.agent_card is not None
    assert parsed.agent_card.agent_id == "fr016-rt"
    skill = parsed.agent_card.skills[0]
    assert (skill.id, skill.scope) == ("ping", "tools:read")


def test_register_agent_parses_old_style_payload_without_api_key():
    """Backward compatibility: a pre-028 RegisterAgent payload (no api_key
    member at all) still parses, with api_key defaulting to None."""
    legacy = json.dumps({
        "type": "register_agent",
        "agent_card": _make_card("fr016-legacy").to_dict(),
    })
    parsed = RegisterAgent.from_json(legacy)
    assert parsed.api_key is None
    assert parsed.agent_card.agent_id == "fr016-legacy"


# ---------------------------------------------------------------------------
# 2. Orchestrator.register_agent enforcement matrix
# ---------------------------------------------------------------------------

def test_keyless_registration_refused_when_env_unset(monkeypatch):
    """028 FR-016 (a): unset ASTRAL_ENV == production — a keyless
    registration with no AGENT_API_KEY configured is refused fail-closed:
    no state mutates and the agent socket closes with policy-violation 1008."""
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    fake = _make_fake()
    ws = _FakeAgentWS()

    asyncio.run(fake.register_agent(ws, RegisterAgent(agent_card=_make_card())))

    _assert_no_registration_state(fake)
    assert ws.close_calls == [(1008, "agent authentication required")]


def test_keyless_registration_allowed_in_declared_dev_mode(monkeypatch):
    """028 FR-016 (b) / spec A13: explicitly declared development mode keeps
    keyless local agents working — registration proceeds end to end."""
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    fake = _make_fake()
    ws = _FakeAgentWS()
    card = _make_card("fr016-dev")

    asyncio.run(fake.register_agent(ws, RegisterAgent(agent_card=card)))

    _assert_registered(fake, ws, card)


def test_matching_key_registers_in_production(monkeypatch):
    """028 FR-016 (c): with AGENT_API_KEY configured, a registration
    presenting the matching key proceeds even with ASTRAL_ENV unset."""
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv("AGENT_API_KEY", "secret123")
    fake = _make_fake()
    ws = _FakeAgentWS()
    card = _make_card("fr016-keyed")

    asyncio.run(fake.register_agent(
        ws, RegisterAgent(agent_card=card, api_key="secret123")))

    _assert_registered(fake, ws, card)


def test_wrong_key_refused_when_key_configured(monkeypatch):
    """028 FR-016 (d): a non-matching presented key is refused before any
    state mutates; the socket closes 1008."""
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv("AGENT_API_KEY", "secret123")
    fake = _make_fake()
    ws = _FakeAgentWS()

    asyncio.run(fake.register_agent(
        ws, RegisterAgent(agent_card=_make_card(), api_key="not-the-key")))

    _assert_no_registration_state(fake)
    assert ws.close_calls == [(1008, "agent authentication required")]


def test_missing_key_refused_when_key_configured(monkeypatch):
    """028 FR-016 (d): omitting the key entirely while AGENT_API_KEY is
    configured is refused — a configured key is binding even in dev mode."""
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("AGENT_API_KEY", "secret123")
    fake = _make_fake()
    ws = _FakeAgentWS()

    asyncio.run(fake.register_agent(ws, RegisterAgent(agent_card=_make_card())))

    _assert_no_registration_state(fake)
    assert ws.close_calls == [(1008, "agent authentication required")]


# ---------------------------------------------------------------------------
# 2b. Ownerless auto-assign default — built-in public, external private
# ---------------------------------------------------------------------------

def _ownerless_fake():
    """Return a fake whose typed ownership registry starts empty."""
    fake = _make_fake()
    registry = _FakeUserAgentRegistry()
    fake.user_agent_registry = registry
    fake._set_ownership_calls = registry.set_calls
    return fake


def test_ownerless_builtin_public_external_private(monkeypatch):
    """With DEFAULT_AGENT_OWNER set, an ownerless registration defaults PUBLIC
    only for a bundled first-party id; every other (external) agent defaults
    PRIVATE — off until an admin turns it on."""
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setenv("DEFAULT_AGENT_OWNER", "op@test")

    fake = _ownerless_fake()
    asyncio.run(fake.register_agent(
        _FakeAgentWS(), RegisterAgent(agent_card=_make_card("weather-1"))))
    asyncio.run(fake.register_agent(
        _FakeAgentWS(), RegisterAgent(agent_card=_make_card("external-x"))))

    by_agent = {aid: is_public for aid, _owner, is_public in fake._set_ownership_calls}
    assert by_agent["weather-1"] is True, "bundled first-party agent defaults public"
    assert by_agent["external-x"] is False, "external agent defaults private (off)"


# ---------------------------------------------------------------------------
# 3. Source-level — base agent presents the env key
# ---------------------------------------------------------------------------

def test_base_agent_sends_env_api_key_in_register_agent():
    """028 FR-016: shared/base_agent.py builds its RegisterAgent with
    api_key=os.getenv("AGENT_API_KEY"), so every first-party agent
    participates in the handshake without per-agent changes."""
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "shared", "base_agent.py"))
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    pattern = (r"RegisterAgent\([^)]*api_key\s*=\s*"
               r"os\.getenv\(\s*['\"]AGENT_API_KEY['\"]\s*\)")
    assert re.search(pattern, source), (
        "base_agent.py must present os.getenv('AGENT_API_KEY') in its "
        "RegisterAgent message (028 FR-016)")
