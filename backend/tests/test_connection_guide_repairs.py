"""First-connection checks remain truthful through probe failure and registration races.
The host uses the same approved component composition for shared rendering and native delivery.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import connection_guide as cg, llm_gate
from orchestrator.projection_surfaces import connection as surface
from tests import test_connection_guide as fixtures

mock_auth = fixtures.mock_auth
production_auth = fixtures.production_auth


async def test_optional_worker_exception_preserves_other_honest_checks(mock_auth, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "conversational_voice", True)
    def broken():
        raise RuntimeError("private worker exception")
    orch = fixtures.FakeOrch(voice_services=SimpleNamespace(worker_pool=SimpleNamespace(readiness=broken)))
    payload = await cg.guide_payload(orch)
    worker = fixtures._check(payload["checks"], "worker")
    assert worker["status"] == "unavailable" and worker["remediation"] and worker["required"] is False
    assert payload["overall"] == "ready" and len(payload["checks"]) == 5
    assert "private worker exception" not in json.dumps(payload)


async def test_auth_exception_does_not_drop_guide_or_expose_authority_secret(production_auth, monkeypatch):
    async def broken(_):
        raise RuntimeError("private identity exception")
    monkeypatch.setattr(cg, "_keycloak_reachable", broken)
    payload = await cg.guide_payload(fixtures.FakeOrch())
    auth = fixtures._check(payload["checks"], "auth")
    assert auth["status"] == "unavailable" and auth["remediation"]
    assert payload["overall"] == "unavailable" and len(payload["checks"]) == 5
    assert "private identity exception" not in json.dumps(payload)


@pytest.mark.parametrize("mode,ready,status", [("off", True, "disabled"), ("audit", True, "degraded"),
    ("enforce", False, "blocked"), ("off", False, "disabled")])
async def test_lets_guide_overall_follows_application_ready(mock_auth, monkeypatch, mode, ready, status):
    monkeypatch.setattr("orchestrator.lets_health_api.lets_snapshot", lambda _: SimpleNamespace(
        mode=mode, application_ready=ready, component_status=status, operator_code="lets_unavailable"))
    payload = await cg.guide_payload(fixtures.FakeOrch())
    assert (payload["overall"] == "ready") is ready
    assert fixtures._check(payload["checks"], "lets")["required"] is (not ready)


async def test_unreadable_lets_is_required_unavailable(mock_auth, monkeypatch):
    def broken(_):
        raise RuntimeError("private LETS details")
    monkeypatch.setattr("orchestrator.lets_health_api.lets_snapshot", broken)
    payload = await cg.guide_payload(fixtures.FakeOrch())
    assert payload["overall"] == "unavailable"
    assert fixtures._check(payload["checks"], "lets")["required"] is True


@pytest.mark.parametrize("value", ["ftp://astral.example", "https://user:secret@astral.example", "https://a\n.example",
    "https://a .example", "https://" + "x"*2048])
def test_endpoint_rejects_invalid_or_credential_bearing_origins(value):
    assert cg._origin_of(value) == ""


def test_ipv6_origin_is_canonical_and_usable():
    assert cg._origin_of("https://[::1]:8001/path") == "https://[::1]:8001"


async def test_shared_render_uses_exact_native_component_view(mock_auth, monkeypatch):
    from astralprojection.models import ChromeViewModel
    captured = []
    def render(view):
        assert isinstance(view, ChromeViewModel)
        captured.append(view)
        return "shared preview"
    monkeypatch.setattr("astralprojection.chrome.render_html", render)
    payload = await cg.guide_payload(fixtures.FakeOrch())
    assert await surface.render_from_payload(payload) == "shared preview"
    assert [item.to_dict() for item in captured[0].components] == await surface.components_from_payload(payload)


async def test_watch_uses_native_chrome_surface(mock_auth):
    orch = fixtures._PushOrch(device_type="watch")
    assert await cg.push_connection_guide(orch, orch.websocket, "user")
    assert json.loads(orch.sent[0])["type"] == "chrome_surface"


@pytest.mark.parametrize("change", ["chat", "gate", "owner", "claims", "registration", "context", "generation", "closing", "pending"])
async def test_registration_changes_during_checks_cannot_clobber_current_ui(mock_auth, monkeypatch, change):
    orch = fixtures._PushOrch()
    entered, release = asyncio.Event(), asyncio.Event()
    async def paused(_):
        entered.set()
        await release.wait()
        return {"overall": "unavailable"}
    monkeypatch.setattr(cg, "guide_payload", paused)
    task = asyncio.create_task(cg.maybe_push_on_register(orch, orch.websocket, "user"))
    await entered.wait()
    context = orch._connection_contexts[id(orch.websocket)]
    if change == "chat":
        orch._ws_active_chat = {id(orch.websocket): "resumed-chat"}
    elif change == "gate":
        llm_gate._gated_map(orch)[id(orch.websocket)] = True
    elif change == "owner":
        orch.ui_sessions[orch.websocket]["sub"] = "other"
    elif change == "claims":
        orch.ui_sessions[orch.websocket]["new_claim"] = True
    elif change == "registration":
        orch.ui_sessions[orch.websocket] = {"sub": "user"}
    elif change == "context":
        orch._connection_contexts[id(orch.websocket)] = SimpleNamespace(**vars(context))
    elif change == "generation":
        context.connection_generation = "generation-2"
    elif change == "closing":
        context.closing = True
    else:
        context.work_registrations_pending = 2
    release.set()
    assert await task is False
    assert orch.sent == [] and not getattr(orch, "_open_chrome_surface", {})


async def test_missing_registered_owner_fails_closed(mock_auth):
    orch = fixtures._PushOrch()
    orch.ui_sessions.clear()
    assert await cg.push_connection_guide(orch, orch.websocket, "user") is False
    assert await cg.maybe_push_on_register(orch, orch.websocket, "user") is False
    assert orch.sent == []


async def test_owner_change_during_send_does_not_mark_new_owner_surface(mock_auth):
    orch = fixtures._PushOrch()
    async def replaced(_, frame):
        orch.ui_sessions[orch.websocket] = {"sub": "other"}
        return True
    orch._safe_send = replaced
    assert await cg.push_connection_guide(orch, orch.websocket, "user") is False
    assert not getattr(orch, "_open_chrome_surface", {})


async def test_initial_pending_registration_publishes_once_before_delivery(mock_auth, monkeypatch):
    orch = fixtures._PushOrch()
    context = orch._connection_contexts[id(orch.websocket)]
    context.registered, context.connection_generation, context.work_registrations_pending = False, None, 1
    async def publish(_):
        context.registered, context.connection_generation = True, "first-generation"
        return {"overall": "unavailable"}
    monkeypatch.setattr(cg, "guide_payload", publish)
    assert await cg.maybe_push_on_register(orch, orch.websocket, "user")
    assert len(orch.sent) == 1


async def test_direct_helper_rechecks_owner_after_component_render(mock_auth, monkeypatch):
    orch = fixtures._PushOrch()
    async def replaced(_):
        orch.ui_sessions[orch.websocket] = {"sub": "other"}
        return "rendered body"
    monkeypatch.setattr(surface, "render_from_payload", replaced)
    assert await cg.push_connection_guide(orch, orch.websocket, "user", payload={}) is False
    assert orch.sent == []


async def test_direct_helper_rechecks_owner_after_probe(mock_auth, monkeypatch):
    orch = fixtures._PushOrch()
    async def replaced(_):
        orch.ui_sessions[orch.websocket] = {"sub": "other"}
        return {"overall": "unavailable"}
    monkeypatch.setattr(cg, "guide_payload", replaced)
    assert await cg.push_connection_guide(orch, orch.websocket, "user") is False
    assert orch.sent == []


async def test_probe_timeout_preserves_bounded_unavailable_and_cancellation(mock_auth, monkeypatch):
    entered = asyncio.Event()
    async def blocked(_):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(cg, "_auth_check", blocked)
    monkeypatch.setattr(cg, "PROBE_TIMEOUT_SECONDS", 0)
    payload = await cg.guide_payload(fixtures.FakeOrch())
    assert fixtures._check(payload["checks"], "auth")["status"] == "unavailable"
    assert len(payload["checks"]) == 5
    monkeypatch.setattr(cg, "PROBE_TIMEOUT_SECONDS", 4)
    entered.clear()
    task = asyncio.create_task(cg.guide_payload(fixtures.FakeOrch()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_html_shared_renderer_escapes_status_and_keeps_refresh_action(mock_auth):
    payload = await cg.guide_payload(fixtures.FakeOrch())
    payload["endpoint"]["guidance"] = '<script>private</script>'
    payload["checks"][0]["detail"] = '<img src=x onerror=alert(1)>'
    html = await surface.render_from_payload(payload)
    assert '<script>' not in html and '<img src=' not in html
    assert '&lt;script&gt;' in html and 'Re-run checks' in html and 'chrome_open' in html


async def test_shared_status_text_retains_readiness_without_color_only_meaning(mock_auth):
    payload = await cg.guide_payload(fixtures.FakeOrch())
    components = await surface.components_from_payload(payload)
    database = next(item for item in components if item.get("type") == "alert" and item.get("title") == "Database")
    assert "Ready" in database["message"]
