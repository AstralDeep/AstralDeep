"""Tests for the first-connection guide (orchestrator/connection_guide.py and
projection_surfaces/connection.py): endpoint attestation, honest per-service checks,
the /api/connection/status route, surface delivery, and the registration-time push.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import connection_guide as cg
from orchestrator.projection_surfaces import connection as connection_surface


def _b64(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _worker_readiness(**overrides):
    values = {
        "ready": True, "reason": "ready", "worker_count": 2,
        "capacity_total": 8, "capacity_available": 5, "profile": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeOrch:
    def __init__(self, *, plane_ok=True, publication_ready=True,
                 voice_services="absent", worker_readiness=None):
        self.plane_ok = plane_ok
        self.publication_ready = publication_ready
        self.voice_services = voice_services
        self.worker_readiness = worker_readiness

    def _probe_plane_readiness(self):
        if not self.plane_ok:
            raise RuntimeError("AstralPlane runtime is not ready")

    class generated_agent_publication_service:
        @staticmethod
        async def readiness():
            return SimpleNamespace(ready=True)


def _publication_service(ready: bool):
    class _Service:
        @staticmethod
        async def readiness():
            return SimpleNamespace(ready=ready)
    return _Service


@pytest.fixture()
def mock_auth(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)


@pytest.fixture()
def production_auth(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://sso.example/realms/Astral")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)


def _check(checks, key):
    return next(c for c in checks if c["key"] == key)


async def test_all_required_services_ready_reports_ready(mock_auth):
    orch = FakeOrch()
    checks = await cg.service_checks(orch)
    assert _check(checks, "database")["status"] == cg.STATUS_READY
    assert _check(checks, "auth")["status"] == cg.STATUS_READY
    assert _check(checks, "publication")["status"] == cg.STATUS_READY
    assert cg._overall(checks) == cg.STATUS_READY


async def test_database_failure_reports_unavailable_with_remediation(mock_auth):
    orch = FakeOrch(plane_ok=False)
    checks = await cg.service_checks(orch)
    database = _check(checks, "database")
    assert database["status"] == cg.STATUS_UNAVAILABLE
    assert database["remediation"]
    assert "PostgreSQL" in database["remediation"]
    assert cg._overall(checks) == cg.STATUS_UNAVAILABLE


async def test_database_failure_recovers_when_probe_passes_again(mock_auth):
    orch = FakeOrch(plane_ok=False)
    first = _check(await cg.service_checks(orch), "database")
    assert first["status"] == cg.STATUS_UNAVAILABLE
    orch.plane_ok = True
    second = _check(await cg.service_checks(orch), "database")
    assert second["status"] == cg.STATUS_READY
    assert cg._overall(await cg.service_checks(orch)) == cg.STATUS_READY


async def test_mock_authentication_is_labeled_development_only(mock_auth):
    check = await cg._auth_check(FakeOrch())
    assert check["status"] == cg.STATUS_READY
    assert "mock" in check["detail"].lower()
    assert "never used in production" in check["detail"]


async def test_unconfigured_identity_provider_reports_unavailable(production_auth, monkeypatch):
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "")
    check = await cg._auth_check(FakeOrch())
    assert check["status"] == cg.STATUS_UNAVAILABLE
    assert check["remediation"]


async def test_unreachable_identity_provider_reports_unavailable(
    production_auth, monkeypatch,
):
    async def _unreachable(authority: str) -> bool:
        return False

    monkeypatch.setattr(cg, "_keycloak_reachable", _unreachable)
    check = await cg._auth_check(FakeOrch())
    assert check["status"] == cg.STATUS_UNAVAILABLE
    assert "unreachable" in check["detail"]


async def test_reachable_identity_provider_reports_ready(production_auth, monkeypatch):
    seen = []

    async def _reachable(authority: str) -> bool:
        seen.append(authority)
        return True

    monkeypatch.setattr(cg, "_keycloak_reachable", _reachable)
    check = await cg._auth_check(FakeOrch())
    assert check["status"] == cg.STATUS_READY
    assert seen == ["https://sso.example/realms/Astral"]


async def test_voice_flag_off_reports_optional_off(mock_auth, monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "conversational_voice", False)
    check = cg._worker_check(FakeOrch())
    assert check["status"] == cg.STATUS_OFF
    assert check["required"] is False


async def test_voice_enabled_without_services_reports_unavailable(mock_auth):
    check = cg._worker_check(FakeOrch(voice_services=None))
    assert check["status"] == cg.STATUS_UNAVAILABLE
    assert check["remediation"]


async def test_client_local_speech_needs_no_worker(mock_auth):
    services = SimpleNamespace(
        backend_selection=SimpleNamespace(value="client_local", valid=True),
        worker_pool=None,
    )
    check = cg._worker_check(FakeOrch(voice_services=services))
    assert check["status"] == cg.STATUS_OFF
    assert "no server voice worker" in check["detail"]


async def test_invalid_speech_backend_fails_voice_closed(mock_auth):
    services = SimpleNamespace(
        backend_selection=SimpleNamespace(value=None, valid=False),
        worker_pool=None,
    )
    check = cg._worker_check(FakeOrch(voice_services=services))
    assert check["status"] == cg.STATUS_UNAVAILABLE
    assert "typed chat is unaffected" in check["detail"]


async def test_connected_voice_workers_report_counts(mock_auth):
    services = SimpleNamespace(
        backend_selection=SimpleNamespace(value="llm_factory", valid=True),
        worker_pool=SimpleNamespace(readiness=lambda: _worker_readiness()),
    )
    check = cg._worker_check(FakeOrch(voice_services=services))
    assert check["status"] == cg.STATUS_READY
    assert "2 voice worker(s)" in check["detail"]


async def test_voice_workers_at_capacity_report_degraded(mock_auth):
    services = SimpleNamespace(
        backend_selection=SimpleNamespace(value="llm_factory", valid=True),
        worker_pool=SimpleNamespace(readiness=lambda: _worker_readiness(
            ready=False, reason="capacity_exhausted", capacity_available=0)),
    )
    check = cg._worker_check(FakeOrch(voice_services=services))
    assert check["status"] == cg.STATUS_DEGRADED
    assert "capacity" in check["detail"]


async def test_missing_voice_worker_reports_degraded_with_remediation(mock_auth):
    services = SimpleNamespace(
        backend_selection=SimpleNamespace(value="llm_factory", valid=True),
        worker_pool=SimpleNamespace(readiness=lambda: _worker_readiness(
            ready=False, reason="worker_unavailable", worker_count=0,
            capacity_total=0, capacity_available=0)),
    )
    check = cg._worker_check(FakeOrch(voice_services=services))
    assert check["status"] == cg.STATUS_DEGRADED
    assert "docker compose up voice-worker" in check["remediation"]


async def test_worker_problem_does_not_block_required_overall(mock_auth):
    services = SimpleNamespace(
        backend_selection=SimpleNamespace(value="llm_factory", valid=True),
        worker_pool=SimpleNamespace(readiness=lambda: _worker_readiness(
            ready=False, reason="worker_unavailable", worker_count=0,
            capacity_total=0, capacity_available=0)),
    )
    checks = await cg.service_checks(FakeOrch(voice_services=services))
    assert _check(checks, "worker")["status"] == cg.STATUS_DEGRADED
    assert cg._overall(checks) == cg.STATUS_READY


async def test_publication_not_ready_reports_unavailable(mock_auth, monkeypatch):
    monkeypatch.setattr(
        FakeOrch, "generated_agent_publication_service", _publication_service(False))
    checks = await cg.service_checks(FakeOrch())
    assert _check(checks, "publication")["status"] == cg.STATUS_UNAVAILABLE
    assert cg._overall(checks) == cg.STATUS_UNAVAILABLE


async def test_publication_failure_probe_reports_unavailable(mock_auth, monkeypatch):
    class _Service:
        @staticmethod
        async def readiness():
            raise RuntimeError("publication store unreachable")

    monkeypatch.setattr(FakeOrch, "generated_agent_publication_service", _Service)
    checks = await cg.service_checks(FakeOrch())
    assert _check(checks, "publication")["status"] == cg.STATUS_UNAVAILABLE


async def test_missing_publication_service_reports_unavailable(mock_auth, monkeypatch):
    orch = FakeOrch()
    orch.generated_agent_publication_service = None
    checks = await cg.service_checks(orch)
    assert _check(checks, "publication")["status"] == cg.STATUS_UNAVAILABLE


async def test_lets_off_reports_optional_off(mock_auth, monkeypatch):
    from types import SimpleNamespace as _NS

    monkeypatch.setattr(
        "orchestrator.lets_health_api.lets_snapshot",
        lambda orch: _NS(mode="off", component_status="disabled",
                         application_ready=False, operator_code="off",
                         existing_behavior_permitted=True,
                         governed_dispatch_ready=False, retryable=False,
                         observed_at_ns=None, diagnostic_only=False),
    )
    check = cg._lets_check(FakeOrch())
    assert check["status"] == cg.STATUS_OFF


async def test_lets_healthy_enforce_reports_ready(mock_auth, monkeypatch):
    from types import SimpleNamespace as _NS

    monkeypatch.setattr(
        "orchestrator.lets_health_api.lets_snapshot",
        lambda orch: _NS(mode="enforce", component_status="healthy",
                         application_ready=True, operator_code="ready",
                         existing_behavior_permitted=True,
                         governed_dispatch_ready=True, retryable=False,
                         observed_at_ns=1, diagnostic_only=False),
    )
    check = cg._lets_check(FakeOrch())
    assert check["status"] == cg.STATUS_READY


async def test_lets_blocked_reports_unavailable(mock_auth, monkeypatch):
    from types import SimpleNamespace as _NS

    monkeypatch.setattr(
        "orchestrator.lets_health_api.lets_snapshot",
        lambda orch: _NS(mode="enforce", component_status="blocked",
                         application_ready=False, operator_code="warden_unreachable",
                         existing_behavior_permitted=False,
                         governed_dispatch_ready=False, retryable=True,
                         observed_at_ns=1, diagnostic_only=False),
    )
    check = cg._lets_check(FakeOrch())
    assert check["status"] == cg.STATUS_UNAVAILABLE
    assert "warden_unreachable" in check["detail"]


async def test_lets_snapshot_failure_reports_degraded(mock_auth, monkeypatch):
    def _boom(orch):
        raise RuntimeError("posture unavailable")

    monkeypatch.setattr("orchestrator.lets_health_api.lets_snapshot", _boom)
    check = cg._lets_check(FakeOrch())
    assert check["status"] == cg.STATUS_DEGRADED


class _FakeEndpoint:
    def __init__(self, configured_env):
        self.configured_env = configured_env


def test_endpoint_origin_normalization():
    assert cg._origin_of("http://localhost:8001") == "http://localhost:8001"
    assert cg._origin_of("https://astral.example") == "https://astral.example"
    assert cg._origin_of("astral.example:8443") == "http://astral.example:8443"
    assert cg._origin_of("http://LOCALHOST:80") == "http://localhost"
    assert cg._origin_of("") == ""
    assert cg._origin_of("http://[::1:bad") == ""


def test_endpoint_report_verified_match(monkeypatch):
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8001")
    report = cg.endpoint_report("http://localhost:8001")
    assert report["verified"] is True
    assert report["configured"] == ["http://localhost:8001"]
    assert "sign-in only on this server's own sign-in page" in report["guidance"]


def test_endpoint_report_mismatch_is_reported_honestly(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://astral.example")
    report = cg.endpoint_report("http://localhost:8001")
    assert report["verified"] is False
    assert "https://astral.example" in report["guidance"]
    assert "http://localhost:8001" in report["guidance"]


def test_endpoint_report_without_configuration_names_the_compose_default(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    report = cg.endpoint_report(None)
    assert report["configured"] == []
    assert report["verified"] is False
    assert "http://localhost:8001" in report["guidance"]


def test_endpoint_report_uses_both_configured_variables(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://astral.example")
    monkeypatch.setenv("BACKEND_PUBLIC_URL", "http://localhost:8001")
    report = cg.endpoint_report("http://localhost:8001")
    assert report["verified"] is True
    assert report["configured"] == ["https://astral.example", "http://localhost:8001"]


async def test_guide_payload_shape_and_user_key_guidance(mock_auth):
    payload = await cg.guide_payload(FakeOrch(), "http://localhost:8001")
    assert payload["contract"] == cg.CONTRACT
    assert payload["surface_key"] == cg.SURFACE_KEY
    assert payload["overall"] == cg.STATUS_READY
    assert payload["endpoint"]["connected"] == "http://localhost:8001"
    assert payload["sign_in"]["steps"]
    joined = " ".join(payload["sign_in"]["steps"]).lower()
    assert "agents never" in joined
    assert "encrypt" in joined


def _status_app(orch) -> TestClient:
    app = FastAPI()
    app.include_router(cg.connection_router)
    app.state.orchestrator = orch
    return TestClient(app)


def test_connection_status_requires_authentication(mock_auth):
    with _status_app(FakeOrch()) as client:
        response = client.get("/api/connection/status")
    assert response.status_code == 401


def test_connection_status_success(mock_auth):
    with _status_app(FakeOrch()) as client:
        response = client.get(
            "/api/connection/status", headers={"Authorization": "Bearer dev-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["overall"] == cg.STATUS_READY
    assert body["endpoint"]["connected"] == "http://testserver"
    assert {c["key"] for c in body["checks"]} == {
        "database", "auth", "worker", "publication", "lets"}


def test_connection_status_reports_failure_as_503_with_honest_body(mock_auth):
    with _status_app(FakeOrch(plane_ok=False)) as client:
        response = client.get(
            "/api/connection/status", headers={"Authorization": "Bearer dev-token"})
    assert response.status_code == 503
    body = response.json()
    assert body["overall"] == cg.STATUS_UNAVAILABLE
    assert _check(body["checks"], "database")["status"] == cg.STATUS_UNAVAILABLE


def test_connection_status_recovers_after_service_returns(mock_auth):
    orch = FakeOrch(plane_ok=False)
    with _status_app(orch) as client:
        failed = client.get(
            "/api/connection/status", headers={"Authorization": "Bearer dev-token"})
        assert failed.status_code == 503
        orch.plane_ok = True
        recovered = client.get(
            "/api/connection/status", headers={"Authorization": "Bearer dev-token"})
    assert recovered.status_code == 200
    assert recovered.json()["overall"] == cg.STATUS_READY


async def test_surface_render_ready_payload_has_no_banner(mock_auth):
    payload = await cg.guide_payload(FakeOrch())
    html = await connection_surface.render_from_payload(payload)
    assert "Database" in html
    assert "Authentication" in html
    assert "Endpoint" in html
    assert "required service is not ready" not in html


async def test_surface_render_failure_payload_shows_banner_and_remediation(mock_auth):
    payload = await cg.guide_payload(FakeOrch(plane_ok=False))
    html = await connection_surface.render_from_payload(payload)
    assert "required service is not ready" in html
    assert "docker compose ps postgres" in html


async def test_surface_components_carry_checks_and_refresh_action(mock_auth):
    payload = await cg.guide_payload(FakeOrch(plane_ok=False))
    comps = await connection_surface.components_from_payload(payload)
    types = [c.get("type") for c in comps]
    assert "alert" in types
    assert "card" in types
    buttons = [c for c in comps if c.get("type") == "button"]
    assert buttons and buttons[-1]["action"] == "chrome_open"
    labels = [c.get("title") or "" for c in comps if c.get("type") == "alert"]
    assert any("Database" in label for label in labels)


async def test_surface_components_for_ready_payload_skip_banner(mock_auth):
    payload = await cg.guide_payload(FakeOrch())
    comps = await connection_surface.components_from_payload(payload)
    assert all(
        not (c.get("type") == "alert" and c.get("title") == "Connection status")
        for c in comps)


async def test_surface_render_and_components_use_params_origin(mock_auth):
    payload_origin = None

    async def _spy(orch, origin=None):
        nonlocal payload_origin
        payload_origin = origin
        return await cg.guide_payload(orch, origin)

    monkeypatched = connection_surface
    original = monkeypatched.guide_payload
    try:
        monkeypatched.guide_payload = _spy
        await connection_surface.render(FakeOrch(), "u", [], {"origin": "http://x:1"})
        assert payload_origin == "http://x:1"
        payload_origin = None
        await connection_surface.components(FakeOrch(), "u", [], {})
        assert payload_origin is None
    finally:
        monkeypatched.guide_payload = original


async def test_recheck_button_uses_manifested_chrome_open_action(mock_auth):
    payload = await cg.guide_payload(FakeOrch())
    comps = await connection_surface.components_from_payload(payload)
    buttons = [c for c in comps if c.get("type") == "button"]
    assert buttons, "the guide must offer a re-check control"
    recheck = buttons[-1]
    assert recheck["action"] == "chrome_open"
    assert recheck["payload"] == {"surface": cg.SURFACE_KEY}


def _fake_send_recorder(delivered=True):
    sent = []

    async def _send(_websocket, payload):
        sent.append(payload)
        return delivered

    return sent, _send


class _PushOrch:
    def __init__(self, delivered=True, device_type="browser"):
        self.delivered = delivered
        self.device_type = device_type
        self.sent, self._send = _fake_send_recorder(delivered)
        self.open_surfaces = {}
        self._probe_plane_readiness = lambda: None
        self.generated_agent_publication_service = _publication_service(True
        )
        self.rote = SimpleNamespace(get_profile=self.get_profile)

    async def _safe_send(self, websocket, payload):
        return await self._send(websocket, payload)

    def get_profile(self, websocket):
        from rote.capabilities import DeviceProfile

        return DeviceProfile.from_dict({"device_type": self.device_type})


async def test_push_connection_guide_delivers_web_modal(mock_auth):
    orch = _PushOrch()
    payload = await cg.guide_payload(orch)
    delivered = await cg.push_connection_guide(orch, object(), "user", payload=payload)
    assert delivered is True
    assert len(orch.sent) == 1
    frame = json.loads(orch.sent[0])
    assert frame["type"] == "chrome_render"
    assert frame["region"] == "modal"
    assert 'data-mandatory="1"' in frame["html"]
    assert "Database" in frame["html"]


async def test_push_connection_guide_delivers_native_surface(mock_auth):
    orch = _PushOrch(device_type="windows")
    payload = await cg.guide_payload(orch)
    delivered = await cg.push_connection_guide(orch, object(), "user", payload=payload)
    assert delivered is True
    frame = json.loads(orch.sent[0])
    assert frame["type"] == "chrome_surface"
    assert frame["surface_key"] == cg.SURFACE_KEY
    assert frame["mode"] == "mandatory"
    assert any(c.get("type") == "card" for c in frame["components"])


async def test_push_connection_guide_notes_open_surface(mock_auth):
    orch = _PushOrch()
    websocket = object()
    await cg.push_connection_guide(orch, websocket, "user")
    assert (getattr(orch, "_open_chrome_surface", None) or {}).get(
        id(websocket)) == cg.SURFACE_KEY


async def test_push_connection_guide_without_delivery_notes_nothing(mock_auth):
    orch = _PushOrch(delivered=False)
    websocket = object()
    delivered = await cg.push_connection_guide(orch, websocket, "user")
    assert delivered is False
    assert id(websocket) not in orch.open_surfaces


async def test_maybe_push_on_register_skips_when_llm_gate_owns_the_modal(mock_auth):

    orch = _PushOrch()
    websocket = object()
    orch._ws_llm_gated = {id(websocket): True}
    pushed = await cg.maybe_push_on_register(orch, websocket, "user")
    assert pushed is False
    assert orch.sent == []


async def test_maybe_push_on_register_skips_with_active_chat(mock_auth):
    orch = _PushOrch()
    websocket = object()
    orch._ws_active_chat = {id(websocket): "chat-1"}
    assert await cg.maybe_push_on_register(orch, websocket, "user") is False
    assert orch.sent == []


async def test_maybe_push_on_register_skips_when_services_ready(mock_auth):
    orch = _PushOrch()
    assert await cg.maybe_push_on_register(orch, object(), "user") is False
    assert orch.sent == []


async def test_maybe_push_on_register_pushes_when_required_service_fails(mock_auth):
    orch = _PushOrch()
    orch._probe_plane_readiness = lambda: (_ for _ in ()).throw(
        RuntimeError("AstralPlane runtime is not ready"))
    pushed = await cg.maybe_push_on_register(orch, object(), "user")
    assert pushed is True
    assert len(orch.sent) == 1


async def test_maybe_push_on_register_swallows_push_failures(mock_auth):
    orch = _PushOrch()
    orch._probe_plane_readiness = lambda: (_ for _ in ()).throw(
        RuntimeError("AstralPlane runtime is not ready"))

    async def _boom(websocket, payload):
        raise RuntimeError("send failed")

    orch._safe_send = _boom
    assert await cg.maybe_push_on_register(orch, object(), "user") is False


async def test_register_ui_pushes_connection_guide_when_required_service_fails(
    monkeypatch,
):
    user_id = f"pytest-connection-guide-{__import__('uuid').uuid4().hex}"
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator.orchestrator import Orchestrator

    try:
        orch = await asyncio.to_thread(Orchestrator)
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    try:
        await orch._llm_store.set(
            user_id, provider="custom", base_url="http://test.invalid/v1",
            model="test-model", api_key="test-key",
        )

        def _plane_down():
            raise RuntimeError("AstralPlane runtime is not ready")

        monkeypatch.setattr(orch, "_probe_plane_readiness", _plane_down)
        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

        task = BackgroundTask(task_id=user_id, chat_id="", user_id=user_id)
        ws = VirtualWebSocket(task)
        orch._registered_events[id(ws)] = asyncio.Event()
        payload = base64.b64encode(json.dumps({
            "sub": user_id, "preferred_username": user_id,
            "realm_access": {"roles": ["admin", "user"]},
        }).encode()).decode().rstrip("=")
        await orch.handle_ui_message(ws, json.dumps({
            "type": "register_ui", "token": f"mock.{payload}.signature",
            "device": {},
        }))
        modal = next(
            (f for f in ws.task.outputs
             if f.get("type") == "chrome_render"
             and 'data-mandatory="1"' in f.get("html", "")
             and "Connection status" in f.get("html", "")),
            None,
        )
        assert modal is not None, "connection guide modal must be pushed"
        assert "required service is not ready" in modal["html"]
    finally:
        try:
            await orch._llm_store.clear(user_id)
        finally:
            await asyncio.wait_for(orch._close_started_services(), timeout=30.0)


async def test_register_ui_connection_guide_push_failure_is_non_fatal(monkeypatch):
    import uuid

    user_id = f"pytest-connection-guide-{uuid.uuid4().hex}"
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator import connection_guide
    from orchestrator.orchestrator import Orchestrator

    try:
        orch = await asyncio.to_thread(Orchestrator)
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    try:
        await orch._llm_store.set(
            user_id, provider="custom", base_url="http://test.invalid/v1",
            model="test-model", api_key="test-key",
        )

        async def _boom(orchestrator, websocket, uid):
            raise RuntimeError("guide evaluation failed")

        monkeypatch.setattr(connection_guide, "maybe_push_on_register", _boom)
        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

        task = BackgroundTask(task_id=user_id, chat_id="", user_id=user_id)
        ws = VirtualWebSocket(task)
        orch._registered_events[id(ws)] = asyncio.Event()
        payload = base64.b64encode(json.dumps({
            "sub": user_id, "preferred_username": user_id,
            "realm_access": {"roles": ["admin", "user"]},
        }).encode()).decode().rstrip("=")
        await orch.handle_ui_message(ws, json.dumps({
            "type": "register_ui", "token": f"mock.{payload}.signature",
            "device": {},
        }))
        assert any(f.get("type") == "rote_config" for f in ws.task.outputs), (
            "registration must complete despite a failed guide push"
        )
    finally:
        try:
            await orch._llm_store.clear(user_id)
        finally:
            await asyncio.wait_for(orch._close_started_services(), timeout=30.0)


def test_overall_counts_required_degraded_between_ready_and_unavailable():
    checks = [
        {"key": "database", "status": cg.STATUS_READY},
        {"key": "auth", "status": cg.STATUS_DEGRADED},
        {"key": "publication", "status": cg.STATUS_READY},
        {"key": "worker", "status": cg.STATUS_UNAVAILABLE},
        {"key": "lets", "status": cg.STATUS_UNAVAILABLE},
    ]
    assert cg._overall(checks) == cg.STATUS_DEGRADED


def test_endpoint_origin_without_host_is_empty():
    assert cg._origin_of("http://") == ""
    assert cg._origin_of("///") == ""


async def test_push_connection_guide_falls_back_to_browser_device_on_profile_error(
    mock_auth,
):
    orch = _PushOrch()
    orch.rote = SimpleNamespace(
        get_profile=lambda websocket: (_ for _ in ()).throw(RuntimeError("no profile")))
    delivered = await cg.push_connection_guide(orch, object(), "user")
    assert delivered is True
    frame = json.loads(orch.sent[0])
    assert frame["type"] == "chrome_render"


async def test_push_connection_guide_native_adaptation_failure_delivers_alert(mock_auth):
    orch = _PushOrch(device_type="windows")
    from rote.adapter import ComponentAdapter

    original = ComponentAdapter.adapt

    def _boom(components, profile):
        raise RuntimeError("adaptation failed")

    try:
        ComponentAdapter.adapt = staticmethod(_boom)
        delivered = await cg.push_connection_guide(orch, object(), "user")
    finally:
        ComponentAdapter.adapt = original
    assert delivered is True
    frame = json.loads(orch.sent[0])
    assert frame["type"] == "chrome_surface"
    assert len(frame["components"]) == 1
    assert frame["components"][0]["type"] == "alert"
    assert "could not be adapted" in frame["components"][0]["message"]


async def test_maybe_push_on_register_swallows_readiness_failures(mock_auth):
    orch = _PushOrch()

    async def _boom(orchestrator, origin=None):
        raise RuntimeError("readiness evaluation failed")

    original = cg.guide_payload
    try:
        cg.guide_payload = _boom
        assert await cg.maybe_push_on_register(orch, object(), "user") is False
    finally:
        cg.guide_payload = original
    assert orch.sent == []


async def test_surface_render_degraded_payload_shows_attention_banner(mock_auth):
    payload = await cg.guide_payload(FakeOrch())
    payload["overall"] = cg.STATUS_DEGRADED
    html = await connection_surface.render_from_payload(payload)
    assert "one or more checks need attention" in html


async def test_surface_components_degraded_payload_shows_warning_alert(mock_auth):
    payload = await cg.guide_payload(FakeOrch())
    payload["overall"] = cg.STATUS_DEGRADED
    comps = await connection_surface.components_from_payload(payload)
    banners = [c for c in comps if c.get("type") == "alert"
               and c.get("title") == "Connection status"]
    assert banners and "need attention" in banners[0]["message"]


async def test_keycloak_reachable_delegates_to_web_auth_discovery(production_auth,
                                                                  monkeypatch):
    from orchestrator import web_auth

    seen = []

    async def _probe(authority: str) -> bool:
        seen.append(authority)
        return True

    monkeypatch.setattr(web_auth, "_idp_reachable", _probe)
    assert await cg._keycloak_reachable("https://sso.example/realms/Astral") is True
    assert seen == ["https://sso.example/realms/Astral"]
