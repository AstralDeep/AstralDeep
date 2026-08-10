"""Inbound authentication for the client-hosted Windows tools agent.

The agent listens on a TCP port (0.0.0.0 by default, so a containerized
orchestrator can reach it) and its tools read/write files and run commands on
the user's PC. Before this gate, ANY host that could reach the port could drive
them — and `_agent_ws` pushed the register frame, which CONTAINS the shared
`AGENT_API_KEY`, to whoever connected before reading a byte.

These tests are the whole coverage story for that gate: nothing anywhere else
exercises `_card` / `_agent_ws` / `_health` / `make_app` over real HTTP.

House style: sync tests driving `asyncio.run` over `aiohttp.test_utils`, because
CI installs only pytest + pytest-cov (no pytest-asyncio, no pytest-aiohttp).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from win_agent import agent as wa  # noqa: E402

GOOD_KEY = "test-agent-key-0123456789abcdef"
HDR = wa.AGENT_KEY_HEADER


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", GOOD_KEY)
    wa._last_refusal_log.clear()
    yield
    wa._last_refusal_log.clear()


def _run(coro):
    return asyncio.run(coro)


async def _with_client(fn, *, key=GOOD_KEY):
    """Serve make_app() and hand a TestClient to ``fn``."""
    os.environ["AGENT_API_KEY"] = key
    server = TestServer(wa.make_app())
    client = TestClient(server)
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# agent card
# --------------------------------------------------------------------------- #


def test_card_without_header_is_401_and_discloses_nothing():
    async def go(client):
        resp = await client.get("/.well-known/agent-card.json")
        return resp.status, await resp.text()

    status, body = _run(_with_client(go))
    assert status == 401
    # No tool inventory, no bypass state, no deployment digests.
    for leak in ("skills", "metadata", "dangerous_bypass", "read_file",
                 "run_command", "deployment_profile_sha256"):
        assert leak not in body, f"401 body leaked {leak!r}"
    assert GOOD_KEY not in body


def test_card_with_correct_header_serves_the_card():
    async def go(client):
        resp = await client.get("/.well-known/agent-card.json",
                                headers={HDR: GOOD_KEY})
        return resp.status, await resp.json()

    status, card = _run(_with_client(go))
    assert status == 200
    assert card["agent_id"] == "windows-tools-1"
    assert card["skills"], "authenticated card must still carry its skills"


def test_card_with_wrong_header_is_401():
    async def go(client):
        resp = await client.get("/.well-known/agent-card.json",
                                headers={HDR: "not-the-key-0123456789abcd"})
        return resp.status

    assert _run(_with_client(go)) == 401


# --------------------------------------------------------------------------- #
# /agent WebSocket — the register frame carries the key, so the gate MUST run
# before the upgrade
# --------------------------------------------------------------------------- #


def test_ws_without_header_is_401_and_never_emits_the_register_frame():
    """The highest-value test here: a gate that ran after ws.prepare() would
    have already handed the shared key to the caller it then rejected."""
    async def go(client):
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect("/agent")
        return exc.value.status

    assert _run(_with_client(go)) == 401


def test_ws_without_header_leaks_no_key_bytes_on_the_socket():
    """Byte-level proof: drive a raw HTTP upgrade and assert the key never
    appears anywhere in the response."""
    async def go(client):
        resp = await client.get("/agent", headers={
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            "Sec-WebSocket-Version": "13",
        })
        return resp.status, await resp.text()

    status, body = _run(_with_client(go))
    assert status == 401
    assert GOOD_KEY not in body
    assert "register_agent" not in body


def test_ws_with_correct_header_upgrades_and_registers():
    async def go(client):
        ws = await client.ws_connect("/agent", headers={HDR: GOOD_KEY})
        raw = await asyncio.wait_for(ws.receive_str(), timeout=5)
        await ws.close()
        return json.loads(raw)

    frame = _run(_with_client(go))
    assert frame["type"] == "register_agent"
    assert frame["agent_card"]["agent_id"] == "windows-tools-1"


# --------------------------------------------------------------------------- #
# header-smuggling and malformed values
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "pairs",
    [
        [(HDR, "wrong-value-0123456789abcd"), (HDR, GOOD_KEY)],
        [(HDR, GOOD_KEY), (HDR, "wrong-value-0123456789abcd")],
    ],
    ids=["wrong-then-right", "right-then-wrong"],
)
def test_duplicate_headers_are_refused_in_both_orders(pairs):
    """Duplicates survive to the handler and `.get()` returns only the FIRST
    (and `getone()` does not raise on them either), so a gate written with
    `.get()` could be fed whichever value it prefers."""
    async def go(client):
        resp = await client.get("/.well-known/agent-card.json", headers=pairs)
        return resp.status

    assert _run(_with_client(go)) == 401


def test_non_ascii_header_is_401_not_500():
    """hmac.compare_digest raises TypeError on non-ASCII str, and this value is
    fully attacker-controlled — inside a handler that would be a 500."""
    async def go(client):
        resp = await client.get("/.well-known/agent-card.json",
                                headers={HDR: "kéy-with-non-ascii-0123456"})
        return resp.status

    assert _run(_with_client(go)) == 401


@pytest.mark.parametrize("value", ["", " ", "\t", GOOD_KEY + " ", GOOD_KEY + "x",
                                   GOOD_KEY[:-1]])
def test_empty_or_near_miss_values_are_refused(value):
    """No normalization on our side: a near-miss is a miss.

    Note LEADING whitespace is deliberately not tested — RFC 7230 makes the OWS
    around a field-value not part of the value, so the HTTP layer strips it
    before the gate ever sees it (verified: `"  x"` arrives as `"x"`, while
    trailing whitespace survives). That is the parser's contract, not a gate
    decision, and an attacker still has to know the exact key either way.
    """
    async def go(client):
        resp = await client.get("/.well-known/agent-card.json", headers={HDR: value})
        return resp.status

    assert _run(_with_client(go)) == 401


def test_401_carries_www_authenticate_and_never_echoes_the_key():
    async def go(client):
        resp = await client.get("/.well-known/agent-card.json",
                                headers={HDR: "wrong-value-0123456789abcd"})
        return resp.status, dict(resp.headers), await resp.text()

    status, headers, body = _run(_with_client(go))
    assert status == 401
    assert "WWW-Authenticate" in headers
    assert "Bearer" not in headers["WWW-Authenticate"], (
        "advertising Bearer invites callers to put the key in a header nothing reads"
    )
    assert HDR not in headers
    assert GOOD_KEY not in json.dumps(headers) + body


# --------------------------------------------------------------------------- #
# /health stays open and stays content-free
# --------------------------------------------------------------------------- #


def test_health_is_open_and_says_only_ok():
    async def go(client):
        resp = await client.get("/health")
        return resp.status, await resp.text()

    status, body = _run(_with_client(go))
    assert status == 200
    assert body == "ok", "health must never grow into a disclosure surface"


# --------------------------------------------------------------------------- #
# refuse to serve at all without a usable key
# --------------------------------------------------------------------------- #


def test_make_app_refuses_without_a_key(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    with pytest.raises(wa.AgentKeyUnavailable):
        wa.make_app()


@pytest.mark.parametrize(
    "key",
    ["short", "change-me", "changeme", "dev-audit-hmac-secret-change-me-in-prod",
     "kéy-non-ascii-but-long-enough"],
    ids=["too-short", "placeholder", "placeholder2", "placeholder3", "non-ascii"],
)
def test_make_app_refuses_a_weak_key(monkeypatch, key):
    """A 4-char key from the first-run dialog would otherwise install a
    trivially guessable gate in front of file-write and command-exec."""
    monkeypatch.setenv("AGENT_API_KEY", key)
    with pytest.raises(wa.AgentKeyUnavailable):
        wa.make_app()


def test_start_agent_thread_returns_none_without_a_key(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    assert wa.start_agent_thread(host="127.0.0.1", port=0) is None


def test_profile_key_beats_the_environment(monkeypatch):
    """A managed profile's credential is authoritative; a decoy in the
    environment must not be accepted."""
    class _Profile:
        managed_agent_api_key = "profile-key-0123456789abcdef"

    monkeypatch.setenv("AGENT_API_KEY", "env-decoy-key-0123456789abc")
    app = wa.make_app(_Profile())
    assert app["inbound_key"] == "profile-key-0123456789abcdef"


# --------------------------------------------------------------------------- #
# source-level and logging guarantees
# --------------------------------------------------------------------------- #


def test_constant_time_comparison_is_used():
    src = open(wa.__file__, encoding="utf-8").read()
    assert "hmac.compare_digest" in src
    assert re.search(r"presented\s*==\s*expected", src) is None


def test_gate_runs_before_ws_prepare():
    """Ordering is the whole point — pin it at source level too, since a future
    edit that moves prepare() above the check would still pass a naive 401 test
    on a wrong key while leaking the register frame on a right one."""
    src = open(wa.__file__, encoding="utf-8").read()
    body = src.split("async def _agent_ws(", 1)[1]
    # Strip comments first: the handler's own comment mentions ws.prepare().
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert code.index("_authorized(request)") < code.index("ws.prepare(")


def test_refusals_never_log_the_key(caplog):
    import logging

    async def go(client):
        await client.get("/.well-known/agent-card.json")
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await client.ws_connect("/agent", headers={HDR: "wrong-0123456789abcdef"})
        return None

    with caplog.at_level(logging.DEBUG):
        _run(_with_client(go))
    blob = "\n".join(
        [r.getMessage() for r in caplog.records] + [repr(r.args) for r in caplog.records]
    )
    assert GOOD_KEY not in blob
    assert "wrong-0123456789abcdef" not in blob


def test_refusal_logging_is_rate_limited(caplog):
    import logging

    async def go(client):
        for _ in range(50):
            await client.get("/.well-known/agent-card.json")
        return None

    with caplog.at_level(logging.WARNING):
        _run(_with_client(go))
    refusals = [r for r in caplog.records if "refused unauthenticated" in r.getMessage()]
    assert 1 <= len(refusals) <= 5, (
        f"50 refusals produced {len(refusals)} log lines — a scanner could flood the log"
    )


def test_both_sides_agree_on_the_header_name():
    """A one-character drift between this constant and the orchestrator's is a
    total, silent outage that BOTH suites would otherwise call green: the
    orchestrator would send a header the agent never reads, and every request
    would 401. Same drift-guard pattern as test_renderer's ui_protocol check.

    Skipped when the backend tree is absent (the windows-client CI job may run
    without it); the backend suite has no counterpart, so this is the only
    place the two constants are ever compared.
    """
    from pathlib import Path

    backend = (Path(__file__).resolve().parents[2] / "backend" / "orchestrator"
               / "agent_peer_auth.py")
    if not backend.is_file():
        pytest.skip("backend tree not present in this checkout")
    m = re.search(r'^AGENT_KEY_HEADER\s*=\s*"([^"]+)"',
                  backend.read_text(encoding="utf-8"), re.M)
    assert m, "the orchestrator no longer declares AGENT_KEY_HEADER"
    assert m.group(1) == HDR, (
        f"header drift: the orchestrator sends {m.group(1)!r}, "
        f"this agent checks {HDR!r} — every request would 401"
    )


def test_refusal_peer_table_is_bounded():
    """The rate limiter keys on source address in a long-lived desktop process;
    unbounded, one entry per distinct address accumulates forever (a local
    process can source all of 127.0.0.0/8). Eviction is oldest-first — the worst
    case is an extra log line from a peer that aged out, never a missed refusal,
    since the gate itself never consults this table."""
    class _Req:
        def __init__(self, ip):
            self.remote = ip

    wa._last_refusal_log.clear()
    for i in range(wa._REFUSAL_LOG_MAX_PEERS * 3):
        wa._log_refusal(_Req(f"10.0.{i // 256}.{i % 256}"), "card")
    assert len(wa._last_refusal_log) <= wa._REFUSAL_LOG_MAX_PEERS


def test_standalone_entry_point_announces_a_non_loopback_bind(caplog):
    """`python -m win_agent.agent` binds through run_app, not start_agent_thread,
    so without its own announcement an operator never sees the warning that the
    open port is guarded."""
    import logging

    with caplog.at_level(logging.WARNING):
        wa._log_bind("0.0.0.0", 8771)
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("reachable from the local network" in m for m in warned)
    assert any(HDR in m for m in warned)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        wa._log_bind("127.0.0.1", 8771)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_main_refuses_without_a_key(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["win_agent.agent"])
    served = []
    monkeypatch.setattr(wa.web, "run_app", lambda *a, **k: served.append(a))
    assert wa.main() == 78
    assert served == [], "main() served the app despite refusing"
