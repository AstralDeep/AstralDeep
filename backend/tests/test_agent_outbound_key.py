"""The orchestrator's OUTBOUND half of the shared agent credential.

``AGENT_API_KEY`` historically authenticated one direction only (a connecting
agent proving itself to the orchestrator — see test_agent_key_enforcement.py).
This file is its mirror: the orchestrator presenting the same secret to agents
it dials out to, so a client-hosted agent (the Windows tools agent, which reads
and writes files and runs commands on a user's PC) can tell this orchestrator
apart from any other host that can reach its port.

The destination gate is the load-bearing part and has its own regression test
below: ``register_external_agent`` accepts a raw user-supplied URL with no
egress validation, and ``AGENT_API_KEY`` is the fleet-wide registration secret,
so sending it to every destination would let any authenticated user make the
orchestrator hand that secret to a host of their choosing.
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


# --------------------------------------------------------------------------- #
# destination gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8005",
        "http://127.0.0.1:8005",
        "http://127.0.0.2:8005",          # anywhere in 127.0.0.0/8
        "http://[::1]:8005",
        "http://host.docker.internal:8771",
        "http://gateway.docker.internal:8771",
    ],
)
def test_key_is_sent_to_trusted_destinations(url):
    """First-party agents, draft subprocesses and the desktop client-hosted
    agent must not regress."""
    assert agent_auth_headers(url) == {AGENT_KEY_HEADER: KEY}


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/agent",
        "http://evil.example:8771",
        "http://192.168.1.50:8771",       # LAN, but not declared
        "http://10.0.0.5:8771",
        "http://169.254.169.254",         # cloud metadata
        "http://localhost.evil.example",  # suffix trick, not localhost
        "http://notlocalhost",
    ],
)
def test_key_is_never_sent_to_an_undeclared_destination(url):
    """SSRF-disclosure regression. register_external_agent takes a raw
    user-supplied URL with no egress validation, and this key can register or
    overwrite ANY agent id — leaking it would be strictly worse than the hole
    the inbound gate closes."""
    assert agent_auth_headers(url) == {}


def test_declared_hosts_are_honored_with_and_without_a_scheme(monkeypatch):
    monkeypatch.setenv("AGENT_KEY_TRUSTED_HOSTS", "desk.lan, http://other.lan:8771")
    assert agent_auth_headers("http://desk.lan:8771") == {AGENT_KEY_HEADER: KEY}
    assert agent_auth_headers("http://other.lan:8771") == {AGENT_KEY_HEADER: KEY}
    assert agent_auth_headers("http://third.lan:8771") == {}


def test_a2a_external_agents_is_not_a_credential_list(monkeypatch):
    """A2A_EXTERNAL_AGENTS is a DISCOVERY list. An operator who wrote a
    third-party partner into it never consented to that partner receiving the
    fleet-wide registration secret, so an upgrade must not silently promote it
    into a credential-distribution list. Trusting such a peer is a separate,
    explicit decision."""
    monkeypatch.setenv("A2A_EXTERNAL_AGENTS", "http://partner.example:9000")
    assert agent_auth_headers("http://partner.example:9000") == {}
    # ...and the explicit opt-in still works.
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


# --------------------------------------------------------------------------- #
# TLS preservation
# --------------------------------------------------------------------------- #


def test_ws_url_preserves_tls():
    """The previous inline expression hardcoded ws:// and stripped https://,
    silently downgrading to plaintext — which would now also put the shared key
    on the wire in the clear."""
    assert agent_ws_url("https://agent.example:8771") == "wss://agent.example:8771/agent"
    assert agent_ws_url("http://agent.example:8771") == "ws://agent.example:8771/agent"
    assert agent_ws_url("http://localhost:8005") == "ws://localhost:8005/agent"


def test_ws_url_preserves_a_path_prefix():
    """An agent mounted behind a reverse proxy keeps its WebSocket transport."""
    assert agent_ws_url("https://host/agents/win") == "wss://host/agents/win/agent"
    assert agent_ws_url("http://host:8771/") == "ws://host:8771/agent"


def test_non_ascii_key_is_refused_rather_than_exploding_in_the_transport(monkeypatch):
    """A header value must be latin-1 encodable; sending a non-ASCII key raises
    inside httpx/aiohttp, and every one of those call sites swallows it at DEBUG
    — so the failure would present as 'the agent silently never registers'."""
    monkeypatch.setenv("AGENT_API_KEY", "kéy-non-ascii-0123456789")
    assert agent_auth_headers("http://localhost:8005") == {}



# --------------------------------------------------------------------------- #
# websockets version compat
# --------------------------------------------------------------------------- #


def test_ws_header_kwarg_matches_the_installed_websockets():
    """The kwarg flipped at websockets 14.0 and the wrong name does NOT raise at
    the call site — it rides **kwargs into loop.create_connection and explodes
    at await time, so a try/except around connect would not work."""
    import inspect

    import websockets
    from shared.ws_compat import WS_HEADER_KWARG, ws_header_kwargs

    params = inspect.signature(websockets.connect).parameters
    assert WS_HEADER_KWARG in params
    assert ws_header_kwargs({"A": "b"}) == {WS_HEADER_KWARG: {"A": "b"}}
    # An empty header map must not send an empty header block.
    assert ws_header_kwargs({}) == {}


def test_ws_compat_probe_detects_the_legacy_kwarg(monkeypatch):
    """Pin the <=13.x branch, which the installed version cannot exercise."""
    import shared.ws_compat as wc

    class _LegacyConnect:
        def __init__(self, uri, *, extra_headers=None, **kwargs):
            pass

    monkeypatch.setattr(wc.websockets, "connect", _LegacyConnect, raising=False)
    assert wc._probe() == "extra_headers"



def _code_only(src: str) -> str:
    """`src` with comments AND string literals removed.

    A line-prefix filter is wrong in both directions: it misses a trailing
    comment (a mention there trips the guard) and it misses docstrings/string
    literals (a mention there escapes it). tokenize gets both right.
    """
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


def test_orchestrator_never_names_a_version_specific_handshake_error():
    """InvalidStatusCode is absent on websockets >= 14 (and merely touching it
    warns); InvalidStatus is absent on the 12.0 legacy path. Only the
    InvalidHandshake base is stable across the supported floor-to-image range.

    Checks CODE, not prose — the comment at the catch site legitimately names
    both classes to explain why neither may be referenced.
    """
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "orchestrator", "orchestrator.py"),
        encoding="utf-8",
    ).read()
    code = _code_only(src)
    assert "InvalidStatusCode" not in code
    assert "exceptions.InvalidStatus " not in code
    assert "exceptions.InvalidStatus(" not in code
    assert "InvalidHandshake" in code


# --------------------------------------------------------------------------- #
# call sites actually carry it
# --------------------------------------------------------------------------- #






# --------------------------------------------------------------------------- #
# Behavioural coverage of discover_agent — these replace the source-regex tests
# that an explanatory COMMENT could satisfy. Each drives the real coroutine
# against a real listener and asserts on what crossed the wire.
# --------------------------------------------------------------------------- #


def _orch():
    """A bare Orchestrator with only what discover_agent touches."""
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
    """A gated agent challenges the unauthenticated probe; the retry carries
    the credential."""
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
    """The same server, addressed by a name the operator never declared."""
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
    """aiohttp forwards request headers across a redirect even to a different
    host/port, so following one would walk the credential straight around the
    destination gate."""
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
    """Otherwise it reads as the 'card not ready yet' startup race and gets
    misdiagnosed as the documented host.docker.internal networking fault."""
    import logging

    from aiohttp import web

    async def card(request):
        # Issues OUR challenge, then refuses the credential anyway: the two
        # keys differ. (A 401 with no challenge means "not an Astral agent"
        # and is covered separately.)
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
    """End to end over a real WebSocket: the header arrives on the handshake and
    the register frame is accepted."""
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


# --------------------------------------------------------------------------- #
# Probe-then-credential: the key is never on the opening request
# --------------------------------------------------------------------------- #


def test_peer_demands_agent_key_parses_the_challenge():
    from orchestrator.agent_peer_auth import peer_demands_agent_key

    assert peer_demands_agent_key('AstralAgentKey realm="win-agent"')
    assert peer_demands_agent_key("astralagentkey")            # case-insensitive
    assert peer_demands_agent_key('Basic realm="x", AstralAgentKey')
    assert not peer_demands_agent_key('Bearer realm="x"')
    assert not peer_demands_agent_key('Basic realm="AstralAgentKey"')  # not the scheme
    assert not peer_demands_agent_key("")
    assert not peer_demands_agent_key(None)


async def test_the_opening_request_never_carries_the_key():
    """Host trust covers a whole machine, so any port on loopback would
    otherwise receive the secret. The first contact is unauthenticated; the key
    only follows OUR challenge."""
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
    """The realistic accidental-disclosure case: some unrelated dev server,
    debug port or database listening on loopback or the Docker host."""
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
    """Both conditions must hold: the peer issued our challenge AND the host is
    operator-declared. An impostor that merely echoes the challenge from an
    undeclared host gets no credential."""
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


# --------------------------------------------------------------------------- #
# log redaction
# --------------------------------------------------------------------------- #


def test_third_party_handshake_logging_is_redacted(caplog):
    """`websockets` logs full handshake headers at DEBUG, so an orchestrator run
    at DEBUG would otherwise write the fleet secret to disk on every agent dial.
    Our own code never logs it — this covers the libraries that do."""
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
    """A very short key is not substring-replaced (it would mangle unrelated
    text); such a key is refused by the agent's own gate anyway."""
    import logging

    from orchestrator.agent_peer_auth import install_key_redaction

    install_key_redaction()
    install_key_redaction()  # idempotent
    monkeypatch.setenv("AGENT_API_KEY", "ab")
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("websockets.client").debug("about to abort")
    assert "about to abort" in caplog.records[-1].getMessage()
