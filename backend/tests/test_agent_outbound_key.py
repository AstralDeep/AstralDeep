"""Tests for the orchestrator's outbound AGENT_API_KEY (agent_peer_auth.py,
Orchestrator.discover_agent/discover_a2a_agent): destination trust gating, TLS/path
preservation, log redaction, and websockets version compatibility.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.agent_peer_auth import (  # noqa: E402
    AGENT_KEY_HEADER,
    agent_auth_headers,
    agent_ws_url,
    trusted_agent_destination,
)

KEY = "outbound-test-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", KEY)
    monkeypatch.delenv("A2A_EXTERNAL_AGENTS", raising=False)
    monkeypatch.delenv("AGENT_KEY_TRUSTED_HOSTS", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8005",
        "http://127.0.0.1:8005",
        "http://127.0.0.2:8005",
        "http://[::1]:8005",
        "http://host.docker.internal:8771",
        "http://gateway.docker.internal:8771",
    ],
)
def test_key_is_sent_to_trusted_destinations(url):
    assert agent_auth_headers(url) == {AGENT_KEY_HEADER: KEY}


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/agent",
        "http://evil.example:8771",
        "http://192.168.1.50:8771",
        "http://10.0.0.5:8771",
        "http://169.254.169.254",
        "http://localhost.evil.example",
        "http://notlocalhost",
    ],
)
def test_key_is_never_sent_to_an_undeclared_destination(url):
    assert agent_auth_headers(url) == {}


def test_declared_hosts_are_honored_with_and_without_a_scheme(monkeypatch):
    monkeypatch.setenv("AGENT_KEY_TRUSTED_HOSTS", "desk.lan, http://other.lan:8771")
    assert agent_auth_headers("http://desk.lan:8771") == {AGENT_KEY_HEADER: KEY}
    assert agent_auth_headers("http://other.lan:8771") == {AGENT_KEY_HEADER: KEY}
    assert agent_auth_headers("http://third.lan:8771") == {}


def test_a2a_external_agents_is_not_a_credential_list(monkeypatch):
    monkeypatch.setenv("A2A_EXTERNAL_AGENTS", "http://partner.example:9000")
    assert agent_auth_headers("http://partner.example:9000") == {}
    monkeypatch.setenv("AGENT_KEY_TRUSTED_HOSTS", "partner.example")
    assert agent_auth_headers("http://partner.example:9000") == {AGENT_KEY_HEADER: KEY}


def test_no_key_configured_means_no_header_anywhere(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    assert agent_auth_headers("http://localhost:8005") == {}
    assert agent_auth_headers("https://evil.example") == {}


@pytest.mark.parametrize("url", ["", "not a url", "http://", "///"])
def test_malformed_urls_are_untrusted(url):
    assert trusted_agent_destination(url) is False
    assert agent_auth_headers(url) == {}


def test_ws_url_preserves_tls():
    assert agent_ws_url("https://agent.example:8771") == "wss://agent.example:8771/agent"
    assert agent_ws_url("http://agent.example:8771") == "ws://agent.example:8771/agent"
    assert agent_ws_url("http://localhost:8005") == "ws://localhost:8005/agent"


def test_ws_url_preserves_a_path_prefix():
    assert agent_ws_url("https://host/agents/win") == "wss://host/agents/win/agent"
    assert agent_ws_url("http://host:8771/") == "ws://host:8771/agent"


def test_non_ascii_key_is_refused_rather_than_exploding_in_the_transport(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "kéy-non-ascii-0123456789")
    assert agent_auth_headers("http://localhost:8005") == {}



def test_ws_header_kwarg_matches_the_installed_websockets():
    import inspect

    import websockets
    from shared.ws_compat import WS_HEADER_KWARG, ws_header_kwargs

    params = inspect.signature(websockets.connect).parameters
    assert WS_HEADER_KWARG in params
    assert ws_header_kwargs({"A": "b"}) == {WS_HEADER_KWARG: {"A": "b"}}
    assert ws_header_kwargs({}) == {}


def test_ws_compat_probe_detects_the_legacy_kwarg(monkeypatch):
    import shared.ws_compat as wc

    class _LegacyConnect:
        def __init__(self, uri, *, extra_headers=None, **kwargs):
            pass

    monkeypatch.setattr(wc.websockets, "connect", _LegacyConnect, raising=False)
    assert wc._probe() == "extra_headers"



def _code_only(src: str) -> str:
    import io as _io
    import tokenize

    out = []
    try:
        for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        return src
    return " ".join(out)


# Only InvalidHandshake is stable across supported websockets versions
def test_orchestrator_never_names_a_version_specific_handshake_error():
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "orchestrator", "orchestrator.py"),
        encoding="utf-8",
    ).read()
    code = _code_only(src)
    assert "InvalidStatusCode" not in code
    assert "exceptions.InvalidStatus " not in code
    assert "exceptions.InvalidStatus(" not in code
    assert "InvalidHandshake" in code








def _orch():
    from orchestrator.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.agents = {}
    o.agent_urls = {}
    return o


async def _serve(routes, port):
    from aiohttp import web

    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


CARD = {
    "name": "Fake", "description": "d", "agent_id": "fake-agent-1",
    "version": "1.0.0", "skills": [], "metadata": {},
}


async def test_card_fetch_carries_the_key_to_a_trusted_destination():
    from aiohttp import web

    seen = []

    async def card(request):
        presented = request.headers.get(AGENT_KEY_HEADER)
        seen.append(presented)
        if presented is None:
            raise web.HTTPUnauthorized(
                headers={"WWW-Authenticate": 'AstralAgentKey realm="win-agent"'})
        return web.json_response(CARD)

    runner = await _serve([web.get("/.well-known/agent-card.json", card)], 9171)
    try:
        await _orch().discover_agent("http://127.0.0.1:9171")
    finally:
        await runner.cleanup()
    assert seen == [None, KEY]


async def test_card_fetch_withholds_the_key_from_an_undeclared_destination():
    from aiohttp import web

    seen = {}

    async def card(request):
        seen["key"] = request.headers.get(AGENT_KEY_HEADER)
        return web.json_response(CARD)

    runner = await _serve([web.get("/.well-known/agent-card.json", card)], 9172)
    try:
        await _orch().discover_agent("http://evil.localtest.me:9172")
    finally:
        await runner.cleanup()
    assert "key" in seen, "the request never arrived — test is not proving anything"
    assert seen["key"] is None


async def test_a_redirect_cannot_carry_the_key_onward(caplog):
    import logging

    from aiohttp import web

    landed = {}

    async def redirector(request):
        raise web.HTTPFound("http://127.0.0.1:9174/.well-known/agent-card.json")

    async def sink(request):
        landed["key"] = request.headers.get(AGENT_KEY_HEADER)
        return web.json_response(CARD)

    a = await _serve([web.get("/.well-known/agent-card.json", redirector)], 9173)
    b = await _serve([web.get("/.well-known/agent-card.json", sink)], 9174)
    try:
        with caplog.at_level(logging.WARNING):
            await _orch().discover_agent("http://127.0.0.1:9173")
    finally:
        await a.cleanup()
        await b.cleanup()
    assert "key" not in landed, "the credential followed a redirect off-host"
    assert any("redirect" in r.getMessage() for r in caplog.records)


async def test_a_401_from_the_agent_logs_an_actionable_warning(caplog):
    import logging

    from aiohttp import web

    async def card(request):
        raise web.HTTPUnauthorized(
            headers={"WWW-Authenticate": 'AstralAgentKey realm="win-agent"'})

    runner = await _serve([web.get("/.well-known/agent-card.json", card)], 9175)
    try:
        with caplog.at_level(logging.WARNING):
            await _orch().discover_agent("http://127.0.0.1:9175")
    finally:
        await runner.cleanup()
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("AGENT_API_KEY must match" in m for m in msgs), msgs


async def test_the_ws_dial_carries_the_key_and_the_agent_registers():
    import json

    from aiohttp import web

    seen = {}

    async def card(request):
        if request.headers.get(AGENT_KEY_HEADER) is None:
            raise web.HTTPUnauthorized(
                headers={"WWW-Authenticate": 'AstralAgentKey realm="win-agent"'})
        return web.json_response(CARD)

    async def agent_ws(request):
        seen["key"] = request.headers.get(AGENT_KEY_HEADER)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({"type": "register_agent", "agent_card": CARD}))
        await ws.receive()
        return ws

    runner = await _serve([
        web.get("/.well-known/agent-card.json", card),
        web.get("/agent", agent_ws),
    ], 9176)
    o = _orch()
    o.register_agent = lambda ws, parsed: _noop()
    o._agent_listen_loop = lambda ws, aid: _noop()
    try:
        await o.discover_agent("http://127.0.0.1:9176")
    finally:
        await runner.cleanup()
    assert seen.get("key") == KEY


async def _noop():
    return None


async def test_key_never_reaches_a_log_record_on_any_discovery_path(caplog):
    import logging

    from aiohttp import web

    async def card(request):
        raise web.HTTPUnauthorized(
            headers={"WWW-Authenticate": 'AstralAgentKey realm="win-agent"'})

    runner = await _serve([web.get("/.well-known/agent-card.json", card)], 9177)
    try:
        with caplog.at_level(logging.DEBUG):
            await _orch().discover_agent("http://127.0.0.1:9177")
            await _orch().discover_agent("http://evil.localtest.me:9177")
    finally:
        await runner.cleanup()
    blob = "\n".join(
        [r.getMessage() for r in caplog.records] + [repr(r.args) for r in caplog.records]
    )
    assert KEY not in blob


def test_peer_demands_agent_key_parses_the_challenge():
    from orchestrator.agent_peer_auth import peer_demands_agent_key

    assert peer_demands_agent_key('AstralAgentKey realm="win-agent"')
    assert peer_demands_agent_key("astralagentkey")
    assert peer_demands_agent_key('Basic realm="x", AstralAgentKey')
    assert not peer_demands_agent_key('Bearer realm="x"')
    assert not peer_demands_agent_key('Basic realm="AstralAgentKey"')
    assert not peer_demands_agent_key("")
    assert not peer_demands_agent_key(None)


async def test_the_opening_request_never_carries_the_key():
    from aiohttp import web

    requests = []

    async def card(request):
        requests.append(request.headers.get(AGENT_KEY_HEADER))
        return web.json_response(CARD)

    runner = await _serve([web.get("/.well-known/agent-card.json", card)], 9181)
    try:
        await _orch().discover_agent("http://127.0.0.1:9181")
    finally:
        await runner.cleanup()
    assert requests == [None], "the key rode on the very first request"


async def test_a_non_astral_service_on_a_trusted_port_never_gets_the_key():
    from aiohttp import web

    requests = []

    async def other(request):
        requests.append(request.headers.get(AGENT_KEY_HEADER))
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="db"'})

    runner = await _serve([web.get("/.well-known/agent-card.json", other)], 9182)
    try:
        await _orch().discover_agent("http://127.0.0.1:9182")
    finally:
        await runner.cleanup()
    assert requests == [None]
    assert all(v is None for v in requests)


async def test_our_challenge_earns_the_key_on_a_trusted_host():
    from aiohttp import web

    requests = []

    async def card(request):
        presented = request.headers.get(AGENT_KEY_HEADER)
        requests.append(presented)
        if presented is None:
            raise web.HTTPUnauthorized(
                headers={"WWW-Authenticate": 'AstralAgentKey realm="win-agent"'})
        return web.json_response(CARD)

    runner = await _serve([web.get("/.well-known/agent-card.json", card)], 9183)
    try:
        await _orch().discover_agent("http://127.0.0.1:9183")
    finally:
        await runner.cleanup()
    assert requests == [None, KEY], f"expected probe-then-credential, got {requests}"


async def test_our_challenge_from_an_undeclared_host_still_gets_nothing(caplog):
    import logging

    from aiohttp import web

    requests = []

    async def card(request):
        requests.append(request.headers.get(AGENT_KEY_HEADER))
        raise web.HTTPUnauthorized(
            headers={"WWW-Authenticate": 'AstralAgentKey realm="win-agent"'})

    runner = await _serve([web.get("/.well-known/agent-card.json", card)], 9184)
    try:
        with caplog.at_level(logging.WARNING):
            await _orch().discover_agent("http://evil.localtest.me:9184")
    finally:
        await runner.cleanup()
    assert requests == [None], "credential sent to an undeclared host"
    assert any("withheld" in r.getMessage() for r in caplog.records)


def test_third_party_handshake_logging_is_redacted(caplog):
    import logging

    from orchestrator.agent_peer_auth import install_key_redaction

    install_key_redaction()
    ws_logger = logging.getLogger("websockets.client")
    with caplog.at_level(logging.DEBUG):
        ws_logger.debug("> GET /agent HTTP/1.1\n> %s: %s", AGENT_KEY_HEADER, KEY)
        ws_logger.debug("raw header line %s", f"{AGENT_KEY_HEADER}: {KEY}")
        ws_logger.debug(f"interpolated {KEY}")
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert KEY not in blob, "the shared key reached a log record in cleartext"
    assert "<redacted:agent-key>" in blob


def test_redaction_leaves_unrelated_records_intact(caplog):
    import logging

    from orchestrator.agent_peer_auth import install_key_redaction

    install_key_redaction()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("websockets.client").debug("connection open to %s", "host:1")
    assert "connection open to host:1" in caplog.records[-1].getMessage()


def test_redaction_is_idempotent_and_survives_a_short_key(monkeypatch, caplog):
    import logging

    from orchestrator.agent_peer_auth import install_key_redaction

    install_key_redaction()
    install_key_redaction()
    monkeypatch.setenv("AGENT_API_KEY", "ab")
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("websockets.client").debug("about to abort")
    assert "about to abort" in caplog.records[-1].getMessage()


@pytest.mark.parametrize("entry", ["http://[", "http://[oops", "]["])
def test_a_malformed_trusted_host_entry_is_ignored_not_trusted(monkeypatch, entry):
    monkeypatch.setenv("AGENT_KEY_TRUSTED_HOSTS", entry)
    assert agent_auth_headers("http://evil.example:8771") == {}
    assert agent_auth_headers("http://localhost:8005") == {AGENT_KEY_HEADER: KEY}


@pytest.mark.parametrize("url", ["http://[", "http://[::1", "http://a[b]c"])
def test_a_malformed_destination_url_is_untrusted(url):
    assert trusted_agent_destination(url) is False
    assert agent_auth_headers(url) == {}


def test_ws_url_tolerates_a_scheme_less_base_url():
    assert agent_ws_url("host:8771") == "ws://host:8771/agent"
    assert agent_ws_url("host:8771/prefix") == "ws://host:8771/prefix/agent"


def test_redaction_handles_dict_style_args(caplog):
    import logging

    from orchestrator.agent_peer_auth import install_key_redaction

    install_key_redaction()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("websockets.client").debug("%(h)s", {"h": f"key={KEY}"})
    assert KEY not in caplog.records[-1].getMessage()


def test_redaction_never_breaks_logging_when_it_raises(monkeypatch, caplog):
    import logging

    import orchestrator.agent_peer_auth as apa

    def _boom(value, key):
        raise RuntimeError("redaction exploded")

    monkeypatch.setattr(apa, "_redact", _boom)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("websockets.client").debug("still logged")
    assert "still logged" in caplog.records[-1].getMessage()


async def test_ws_handshake_401_is_caught_and_logged_actionably(caplog):
    import logging

    from aiohttp import web

    async def card(request):
        return web.json_response(CARD)

    async def agent_ws(request):
        raise web.HTTPUnauthorized(
            headers={"WWW-Authenticate": 'AstralAgentKey realm="win-agent"'})

    runner = await _serve([
        web.get("/.well-known/agent-card.json", card),
        web.get("/agent", agent_ws),
    ], 9186)
    try:
        with caplog.at_level(logging.WARNING):
            await _orch().discover_agent("http://127.0.0.1:9186")
    finally:
        await runner.cleanup()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("refused the orchestrator's credential on /agent" in m for m in msgs), msgs


async def test_a2a_fallback_resolver_is_credentialed_and_bounded(monkeypatch):
    import httpx

    captured = {}
    real_init = httpx.AsyncClient.__init__

    def _spy(self, *a, **kw):
        captured["headers"] = dict(kw.get("headers") or {})
        captured["timeout"] = kw.get("timeout")
        captured["follow_redirects"] = kw.get("follow_redirects")
        return real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _spy)
    o = _orch()
    o.a2a_clients = {}
    o.a2a_agent_cards = {}
    await o.discover_a2a_agent("http://127.0.0.1:9199")
    assert captured.get("timeout") == 5.0
    assert captured.get("follow_redirects") is False
    assert captured.get("headers", {}).get(AGENT_KEY_HEADER) == KEY


async def test_a2a_backup_resolver_is_credentialed_and_bounded(monkeypatch):
    import httpx

    captured = {}
    real_init = httpx.AsyncClient.__init__

    def _spy(self, *a, **kw):
        captured["headers"] = dict(kw.get("headers") or {})
        captured["timeout"] = kw.get("timeout")
        return real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _spy)
    o = _orch()
    o.a2a_clients = {}
    o.a2a_agent_cards = {}
    await o._setup_a2a_client_for_agent("http://127.0.0.1:9199", "fake-agent-1")
    assert captured.get("timeout") == 5.0
    assert captured.get("headers", {}).get(AGENT_KEY_HEADER) == KEY


async def test_execute_via_a2a_carries_the_key_beside_the_delegation_token(monkeypatch):
    import httpx

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"result": {}}

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["headers"] = dict(headers or {})
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    o = _orch()
    o.a2a_clients = {"fake-agent-1": "http://127.0.0.1:9199"}
    await o._execute_via_a2a(
        "fake-agent-1", "do_thing", {"_delegation_token": "tok-123"}
    )
    assert captured["headers"].get(AGENT_KEY_HEADER) == KEY
    assert captured["headers"].get("Authorization") == "Bearer tok-123"
