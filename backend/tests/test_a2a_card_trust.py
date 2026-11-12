"""Covers signed agent card admission for external A2A peers: pinned keys, tampering, change after approval, JWKS fetch, audit and registration."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentCardSignature, AgentInterface, AgentSkill

from orchestrator import a2a_card_trust as trust_module
from orchestrator.a2a_card_trust import CardRefused, CardTrust, CardTrustRegistry
from orchestrator.orchestrator import Orchestrator
from tests.a2a_card_signing import TestSigner

BASE = "https://peer.example"
JWKS = "https://peer.example/.well-known/jwks.json"


def _card(description="test peer") -> AgentCard:
    return AgentCard(
        name="Peer", description=description, version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=[AgentSkill(id="echo", name="echo", description="echo", tags=["t"])],
        default_input_modes=["application/json"], default_output_modes=["application/json"],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=BASE + "/rpc", protocol_version="1.0")],
    )


def _jwks_fetcher(signer, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        return [signer.public_jwk]
    return fetch


def test_signed_card_verifies_against_inline_key():
    signer = TestSigner()
    card = signer.sign(_card())
    digest = trust_module.verify_card(card, CardTrust(public_key=signer.public_jwk))
    assert digest == trust_module.card_digest(card) and digest.startswith("sha256:")


def test_signed_card_verifies_against_pinned_jwks_fetched_through_the_guarded_client():
    signer = TestSigner()
    calls = []
    card = signer.sign(_card(), jku=JWKS)
    trust_module.verify_card(card, CardTrust(jwks_url=JWKS), jwks_fetcher=_jwks_fetcher(signer, calls))
    assert calls == [JWKS]


def test_default_jwks_fetch_goes_through_external_http(monkeypatch):
    signer = TestSigner()
    seen = {}

    def request(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        return SimpleNamespace(json=lambda: {"keys": [signer.public_jwk, {"kty": "oct", "k": "AA"}]})

    monkeypatch.setattr(trust_module.external_http, "request", request)
    assert trust_module.fetch_jwks(JWKS) == [signer.public_jwk]
    assert seen["method"] == "GET" and seen["url"] == JWKS and seen["max_response_bytes"] <= 64 * 1024
    trust_module.verify_card(signer.sign(_card()), CardTrust(jwks_url=JWKS))


@pytest.mark.parametrize("body", [{}, {"keys": []}, {"keys": "x"}, [1]])
def test_malformed_jwks_is_refused_as_unavailable(monkeypatch, body):
    signer = TestSigner()
    monkeypatch.setattr(trust_module.external_http, "request", lambda *a, **k: SimpleNamespace(json=lambda: body))
    with pytest.raises(CardRefused) as refusal:
        trust_module.verify_card(signer.sign(_card()), CardTrust(jwks_url=JWKS))
    assert refusal.value.code == trust_module.KEY_UNAVAILABLE


def test_unreachable_or_egress_blocked_jwks_is_refused(monkeypatch):
    signer = TestSigner()

    def blocked(*args, **kwargs):
        raise trust_module.external_http.EgressBlockedError("private address")

    monkeypatch.setattr(trust_module.external_http, "request", blocked)
    with pytest.raises(CardRefused) as refusal:
        trust_module.verify_card(signer.sign(_card()), CardTrust(jwks_url=JWKS))
    assert refusal.value.code == trust_module.KEY_UNAVAILABLE


def test_unsigned_card_is_refused():
    signer = TestSigner()
    with pytest.raises(CardRefused) as refusal:
        trust_module.verify_card(_card(), CardTrust(public_key=signer.public_jwk))
    assert refusal.value.code == trust_module.UNSIGNED


def test_tampered_content_is_refused():
    signer = TestSigner()
    card = signer.sign(_card())
    card.description = "now asks for your records"
    with pytest.raises(CardRefused) as refusal:
        trust_module.verify_card(card, CardTrust(public_key=signer.public_jwk))
    assert refusal.value.code == trust_module.SIGNATURE_INVALID


def test_signature_by_a_key_that_was_not_pinned_is_refused():
    attacker, owner = TestSigner(), TestSigner()
    with pytest.raises(CardRefused) as refusal:
        trust_module.verify_card(attacker.sign(_card()), CardTrust(public_key=owner.public_jwk))
    assert refusal.value.code == trust_module.SIGNATURE_INVALID


def test_key_id_mismatch_is_refused():
    signer = TestSigner("kid-a")
    other = dict(signer.public_jwk, kid="kid-b")
    with pytest.raises(CardRefused) as refusal:
        trust_module.verify_card(signer.sign(_card()), CardTrust(public_key=other))
    assert refusal.value.code == trust_module.SIGNATURE_INVALID


def test_signature_naming_an_unpinned_key_set_is_refused_without_fetching_it():
    signer = TestSigner()
    calls = []
    card = signer.sign(_card(), jku="https://attacker.example/jwks.json")
    with pytest.raises(CardRefused) as refusal:
        trust_module.verify_card(card, CardTrust(jwks_url=JWKS), jwks_fetcher=_jwks_fetcher(signer, calls))
    assert refusal.value.code == trust_module.SIGNATURE_INVALID and calls == []
    with pytest.raises(CardRefused):
        trust_module.verify_card(card, CardTrust(public_key=signer.public_jwk))


def _forge(card: AgentCard, header: dict, secret: bytes = b"k" * 32) -> AgentCard:
    from a2a.utils.signing import _canonicalize_agent_card

    payload = _canonicalize_agent_card(card).encode()
    token = jwt.api_jws.encode(payload, secret, algorithm=header["alg"], headers=header) if header["alg"] != "none" \
        else ".".join([base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode(),
                       base64.urlsafe_b64encode(payload).rstrip(b"=").decode(), ""])
    protected, _, signature = token.split(".")
    card.signatures.append(AgentCardSignature(protected=protected, signature=signature))
    return card


@pytest.mark.parametrize("alg", ["none", "HS256"])
def test_none_and_symmetric_algorithms_are_refused(alg):
    signer = TestSigner()
    card = _forge(_card(), {"alg": alg, "kid": signer.kid, "typ": "JOSE"})
    with pytest.raises(CardRefused) as refusal:
        trust_module.verify_card(card, CardTrust(public_key=signer.public_jwk))
    assert refusal.value.code == trust_module.SIGNATURE_INVALID


def test_garbled_signature_entries_are_refused():
    signer = TestSigner()
    card = _card()
    card.signatures.append(AgentCardSignature(protected="!!!", signature="x"))
    card.signatures.append(AgentCardSignature(protected=base64.urlsafe_b64encode(b"[1]").decode(), signature="x"))
    with pytest.raises(CardRefused):
        trust_module.verify_card(card, CardTrust(public_key=signer.public_jwk))


def test_any_valid_signature_among_several_is_enough():
    signer, other = TestSigner("a"), TestSigner("b")
    card = other.sign(_card())
    signer.sign(card)
    trust_module.verify_card(card, CardTrust(public_key=signer.public_jwk))


@pytest.mark.parametrize("value", [
    "x", {}, {"jwks_url": "http://peer.example/jwks"}, {"jwks_url": "https://u:p@peer.example/jwks"},
    {"public_key": {"kty": "OKP", "crv": "Ed25519", "x": "AA", "d": "AA"}}, {"public_key": {"kty": "bogus"}},
    {"public_key": "nope"}, {"jwks_url": JWKS, "card_sha256": "md5:zz"},
])
def test_invalid_pin_settings_are_rejected(value):
    with pytest.raises(CardRefused) as refusal:
        CardTrust.from_mapping(value)
    assert refusal.value.code == trust_module.NO_PINNED_KEY


def test_registry_refuses_unpinned_peers_and_changes_after_approval():
    signer = TestSigner()
    registry = CardTrustRegistry()
    with pytest.raises(CardRefused) as refusal:
        registry.admit(BASE, signer.sign(_card()))
    assert refusal.value.code == trust_module.NO_PINNED_KEY
    registry.pin(BASE + "/", CardTrust(public_key=signer.public_jwk))
    first = registry.admit(BASE, signer.sign(_card()))
    assert registry.admit(BASE, signer.sign(_card())) == first
    with pytest.raises(CardRefused) as refusal:
        registry.admit(BASE, signer.sign(_card("a validly signed but different card")))
    assert refusal.value.code == trust_module.CHANGED
    registry.pin(BASE, CardTrust(public_key=signer.public_jwk))
    registry.admit(BASE, signer.sign(_card("approved again")))
    registry.forget(BASE)
    assert registry.trust_for(BASE) is None


def test_owner_supplied_digest_must_match_the_first_card():
    signer = TestSigner()
    approved = trust_module.card_digest(_card())
    registry = CardTrustRegistry()
    registry.pin(BASE, CardTrust(public_key=signer.public_jwk, card_sha256=approved))
    registry.admit(BASE, signer.sign(_card()))
    with pytest.raises(CardRefused) as refusal:
        registry.admit(BASE, signer.sign(_card("different")))
    assert refusal.value.code == trust_module.CHANGED


def test_registry_loads_pins_from_environment_and_skips_bad_entries():
    signer = TestSigner()
    raw = json.dumps({BASE: {"public_key": signer.public_jwk}, "https://bad.example": {"jwks_url": "http://x"}})
    registry = trust_module.registry_from_environment(raw)
    assert registry.trust_for(BASE) is not None and registry.trust_for("https://bad.example") is None
    assert trust_module.registry_from_environment("").trust_for(BASE) is None
    assert trust_module.registry_from_environment("{not json").trust_for(BASE) is None
    assert trust_module.registry_from_environment("[1]").trust_for(BASE) is None


def _orchestrator() -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.a2a_card_trust = CardTrustRegistry()
    return orch


@pytest.fixture
def audit(monkeypatch):
    events = []

    async def record(**event):
        events.append(event)

    monkeypatch.setattr("audit.hooks.record_generic", record)
    return events


@pytest.mark.asyncio
async def test_admission_is_audited_for_success_and_each_refusal(audit):
    signer = TestSigner()
    orch = _orchestrator()
    assert await orch._admit_a2a_card(BASE, signer.sign(_card())) is False
    orch.a2a_card_trust.pin(BASE, CardTrust(public_key=signer.public_jwk))
    assert await orch._admit_a2a_card(BASE, _card()) is False
    assert await orch._admit_a2a_card(BASE, signer.sign(_card())) is True
    assert await orch._admit_a2a_card(BASE, signer.sign(_card("changed"))) is False
    assert [(e["action_type"], e["outputs_meta"]["result"]) for e in audit] == [
        ("a2a.agent_card.refused", "no_pinned_key"), ("a2a.agent_card.refused", "card_unsigned"),
        ("a2a.agent_card.admitted", "admitted"), ("a2a.agent_card.refused", "card_changed_after_approval")]
    assert audit[2]["outputs_meta"]["card_sha256"].startswith("sha256:")
    assert all(e["inputs_meta"]["agent_url"] == BASE for e in audit)


@pytest.mark.asyncio
async def test_discovery_refuses_an_unsigned_card_and_registers_nothing(audit, monkeypatch):
    from aiohttp import web

    signer = TestSigner()
    payload = {"name": "Peer", "description": "d", "version": "1.0.0", "capabilities": {},
               "skills": [{"id": "echo", "name": "echo", "description": "e", "tags": ["t"]}],
               "defaultInputModes": ["a/b"], "defaultOutputModes": ["a/b"],
               "supportedInterfaces": [{"url": "http://127.0.0.1/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]}
    served = {"card": payload}

    async def card(request):
        return web.json_response(served["card"])

    app = web.Application()
    app.add_routes([web.get("/.well-known/agent-card.json", card)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    payload["supportedInterfaces"][0]["url"] = base + "/rpc"
    try:
        from tests.test_a2a_outbound_negotiation import _discovery_orchestrator

        orch = _discovery_orchestrator()
        orch.a2a_card_trust = CardTrustRegistry()
        orch.a2a_card_trust.pin(base, CardTrust(public_key=signer.public_jwk))
        orch.discover_agent = AsyncMock()
        await orch.discover_a2a_agent(base)
        assert orch.a2a_clients == {} and orch.agent_cards == {}
        assert audit[-1]["outputs_meta"]["result"] == "card_unsigned"

        served["card"] = signer.sign_payload(payload)
        await orch.discover_a2a_agent(base)
        assert list(orch.a2a_clients.values()) == [base]
        assert audit[-1]["action_type"] == "a2a.agent_card.admitted"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_register_action_requires_a_pinned_key_and_pins_it(audit):
    signer = TestSigner()
    orch = _orchestrator()
    orch.send_ui_render = AsyncMock()
    orch._safe_send = AsyncMock()
    orch.discover_a2a_agent = AsyncMock()
    orch.send_agent_list = AsyncMock()
    orch.agent_cards = {}
    ws = object()

    await orch._register_external_agent(ws, {"url": ""})
    await orch._register_external_agent(ws, {"url": BASE})
    assert orch.discover_a2a_agent.await_count == 0
    assert orch.send_ui_render.await_count == 2
    assert "Pin a JWKS URL" in orch.send_ui_render.await_args.args[1][0]["message"]

    await orch._register_external_agent(ws, {"url": BASE + "/", "public_key": signer.public_jwk})
    orch.discover_a2a_agent.assert_awaited_once_with(BASE)
    assert orch.a2a_card_trust.trust_for(BASE).public_key == signer.public_jwk
    assert "Could not discover" in orch.send_ui_render.await_args.args[1][0]["message"]

    orch.agent_cards = {"peer": SimpleNamespace(metadata={"a2a_url": BASE})}
    await orch._register_external_agent(ws, {"url": BASE, "jwks_url": JWKS})
    orch.send_agent_list.assert_awaited_once()


def test_registry_is_created_from_the_environment_on_first_use(monkeypatch):
    signer = TestSigner()
    monkeypatch.setenv("A2A_EXTERNAL_AGENT_KEYS", json.dumps({BASE: {"public_key": signer.public_jwk}}))
    orch = Orchestrator.__new__(Orchestrator)
    assert orch._card_trust_registry().trust_for(BASE) is not None
    assert orch._card_trust_registry() is orch.a2a_card_trust
