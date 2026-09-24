"""Tests for the external driver (backend/verification/drivers/external.py) with mocked
httpx/websockets transports: auth-mode degradation, WS message parsing, and a
scenario run against the mocked transport.
"""

from __future__ import annotations

from verification.config import RunConfig
from verification.drivers.external import ExternalDriver, decide_auth_mode, parse_ws_messages
from verification.scenarios import build_scenarios
from verification.tests.conftest import run_async


def _cfg(**kw):
    return RunConfig(mode="external", run_id="__verif__ext", base_url="https://example.test", **kw)


def test_decide_auth_mode_no_credentials(monkeypatch):
    for n in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET"):
        monkeypatch.delenv(n, raising=False)
    mode, flags = decide_auth_mode(_cfg())
    assert mode == "mock_inprocess"
    assert "keycloak_credentials_absent" in flags


def test_decide_auth_mode_unreachable_degrades(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.test")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "cid")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "shh-abc123")
    mode, flags = decide_auth_mode(_cfg(), reachable=False)
    assert mode == "mock_inprocess"
    assert "keycloak_unreachable_degraded" in flags


def test_decide_auth_mode_real(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.test")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "cid")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "shh-abc123")
    mode, flags = decide_auth_mode(_cfg(), reachable=True)
    assert mode == "real_keycloak"
    assert flags == []


def test_parse_ws_messages_filters_and_parses():
    raw = [
        '{"type": "ui_render", "components": [{"type": "table"}]}',
        {"type": "ui_upsert", "ops": []},
        {"type": "noise"},
        "not json",
    ]
    msgs = parse_ws_messages(raw)
    assert [m["type"] for m in msgs] == ["ui_render", "ui_upsert"]


def test_run_scenario_with_mocked_transport(monkeypatch):
    for n in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET"):
        monkeypatch.delenv(n, raising=False)
    http_calls = []

    def _http(method, url, token=None, **kw):
        http_calls.append((method, url, token))
        if url.endswith("/api/upload"):
            return {"attachment_id": "att-123"}
        return {}

    async def _ws(url, token, register, chat):
        assert register["type"] == "register_ui"
        return [{"type": "ui_render", "components": [{"type": "table"}], "html": "<div></div>"}]

    driver = ExternalDriver(_cfg(), http=_http, ws_exchange=_ws)
    run_async(driver.setup())
    scenario = build_scenarios("__verif__ext", driver.auth_mode, ["everyday"])[0]
    ev = run_async(driver.run_scenario(scenario))
    assert ev.run_mode == "mock_inprocess"
    assert any(c.get("type") == "table" for c in ev.components)
    assert ev.extra["attachment_id"] == "att-123"
    assert any(u.endswith("/api/upload") for _m, u, _t in http_calls)
