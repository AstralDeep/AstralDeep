"""Feature 055 (US5, T043) — export REST routes.

``GET /api/export/component/{id}.csv`` and ``GET /api/export/canvas/{chat_id}.html``
behind ``FF_ARTIFACT_EXPORT``:

* ownership via the (chat_id, user_id)-scoped workspace lookup (uniform 404);
* CSV of the stored rows, OWASP formula-injection guard on leading ``=+-@``;
* paginated tables re-invoke the recorded source tool through the
  component_action gate sequence (permission check, full-range ``limit/offset``,
  credential injection) — ``?stored_only=1`` skips the re-invoke, a failed or
  retired source maps to 503 "partial data available";
* canvas export materializes ``_canvas_components``, degrades charts down the
  ROTE fallback ladder, and wraps the static rendition in the self-contained
  export document (provenance note + generation date stamped);
* flag off ⇒ 404 with FastAPI's route-absent body; missing auth ⇒ 401.

Routes are exercised over a real FastAPI app + TestClient with a mocked
orchestrator (test_rest_api.py pattern) — no DB rows are touched, so the
feature-052 loop guard sees no synchronous Database calls at all.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["USE_MOCK_AUTH"] = "true"

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import orchestrator.web_auth as web_auth  # noqa: E402
from orchestrator.api import _csv_body, export_router  # noqa: E402
from shared.feature_flags import flags  # noqa: E402


def _make_mock_token(payload: dict) -> str:
    import base64
    import json as _json
    body = base64.b64encode(_json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


USER_ID = "pytest-export-user"
AUTH = {"Authorization": f"Bearer {_make_mock_token({'sub': USER_ID})}"}

CHAT_ID = "chat-export-1"


def _table_cd(**over):
    cd = {
        "type": "table",
        "component_id": "wc_tbl1",
        "title": "Rows",
        "headers": ["name", "amount"],
        "rows": [["alice", 1], ["bob", 2]],
        "_source_agent": "data-agent-1",
        "_source_tool": "list_rows",
        "_source_params": {"q": "all", "limit": 2, "offset": 0},
        "provenance": "grounded",
    }
    cd.update(over)
    return cd


def _row(cd):
    return {"chat_id": CHAT_ID, "component_id": cd.get("component_id"),
            "component_data": cd}


@pytest.fixture()
def orch():
    m = MagicMock()
    m.workspace = MagicMock()
    m.workspace.aget_by_component_id = AsyncMock(return_value=None)
    m._canvas_components = MagicMock(return_value=[])
    m._component_action_allowed = MagicMock(return_value=(True, ""))
    m.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    m._execute_with_retry = AsyncMock(return_value=None)
    m.execute_authorized_tool = AsyncMock(return_value=None)
    return m


@pytest.fixture()
def client(orch):
    app = FastAPI()
    app.include_router(export_router)
    app.state.orchestrator = orch
    return TestClient(app)


@pytest.fixture(autouse=True)
def export_on():
    prior = flags._flags.get("artifact_export")
    flags._flags["artifact_export"] = True
    yield
    flags._flags["artifact_export"] = prior


def _csv_get(client, component_id="wc_tbl1", **params):
    query = {"chat_id": CHAT_ID, **params}
    return client.get(f"/api/export/component/{component_id}.csv",
                      params=query, headers=AUTH)


# ───────────────────────── CSV: stored rows ──────────────────────────────────


def test_csv_stored_table(client, orch):
    orch.workspace.aget_by_component_id.return_value = _row(_table_cd())
    r = _csv_get(client)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="wc_tbl1.csv"'
    assert r.text.splitlines() == ["name,amount", "alice,1", "bob,2"]
    # A complete stored table never re-invokes the source.
    orch._execute_with_retry.assert_not_awaited()
    orch.workspace.aget_by_component_id.assert_awaited_once_with(
        CHAT_ID, USER_ID, "wc_tbl1")


def test_csv_formula_injection_guard(client, orch):
    cd = _table_cd(headers=["=h", "safe"],
                   rows=[["=SUM(A1)", "+x"], ["-1", "@cmd"], ["ok", 7]])
    orch.workspace.aget_by_component_id.return_value = _row(cd)
    r = _csv_get(client)
    assert r.status_code == 200
    assert r.text.splitlines() == [
        "'=h,safe", "'=SUM(A1),'+x", "'-1,'@cmd", "ok,7"]


def test_csv_body_helper_guards_every_owasp_trigger():
    out = _csv_body(["h"], [["=1"], ["+1"], ["-1"], ["@1"], ["1"], [None]])
    assert out.splitlines() == ["h", "'=1", "'+1", "'-1", "'@1", "1", '""']


def test_csv_not_a_table_is_422(client, orch):
    orch.workspace.aget_by_component_id.return_value = _row(
        {"type": "card", "component_id": "wc_c", "title": "T"})
    r = _csv_get(client, component_id="wc_c")
    assert r.status_code == 422
    assert r.json()["error"] == "not_a_table"


def test_csv_unknown_or_foreign_component_is_uniform_404(client, orch):
    orch.workspace.aget_by_component_id.return_value = None
    assert _csv_get(client, component_id="wc_missing").status_code == 404


def test_csv_requires_chat_id_query(client, orch):
    r = client.get("/api/export/component/wc_tbl1.csv", headers=AUTH)
    assert r.status_code == 422


# ───────────────────────── CSV: paginated full-data re-invoke ────────────────


def _paginated_cd():
    return _table_cd(total_rows=5, page_size=2, page_offset=0)


def _full_result():
    return SimpleNamespace(error=None, ui_components=[{
        "type": "table", "headers": ["name", "amount"],
        "rows": [["alice", 1], ["bob", 2], ["carol", 3], ["dave", 4], ["erin", 5]],
        "total_rows": 5,
    }])


def test_csv_paginated_reinvokes_source_full_range(client, orch):
    orch.workspace.aget_by_component_id.return_value = _row(_paginated_cd())
    orch.execute_authorized_tool.return_value = _full_result()
    r = _csv_get(client)
    assert r.status_code == 200
    lines = r.text.splitlines()
    assert len(lines) == 6 and lines[-1] == "erin,5"
    # Same gate + params the component_action pipeline applies.
    orch._component_action_allowed.assert_called_once_with(
        USER_ID, "data-agent-1", "list_rows")
    call = orch.execute_authorized_tool.await_args.kwargs
    assert (call["agent_id"], call["tool_name"], call["channel"]) == (
        "data-agent-1", "list_rows", "rest")
    args = call["arguments"]
    assert args["limit"] == 5 and args["offset"] == 0 and args["q"] == "all"


def test_csv_stored_only_skips_reinvoke(client, orch):
    orch.workspace.aget_by_component_id.return_value = _row(_paginated_cd())
    r = _csv_get(client, stored_only=1)
    assert r.status_code == 200
    assert len(r.text.splitlines()) == 3  # header + the stored page only
    orch._execute_with_retry.assert_not_awaited()


def test_csv_reinvoke_failure_is_503_partial_data(client, orch):
    orch.workspace.aget_by_component_id.return_value = _row(_paginated_cd())
    orch.execute_authorized_tool.return_value = SimpleNamespace(
        error={"message": "agent down"}, ui_components=[])
    r = _csv_get(client)
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "source_unavailable"
    assert body["detail"] == "partial data available"


def test_csv_permission_denied_is_403(client, orch):
    orch.workspace.aget_by_component_id.return_value = _row(_paginated_cd())
    orch._component_action_allowed.return_value = (
        False, "This tool is disabled in your permissions.")
    r = _csv_get(client)
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"
    orch.execute_authorized_tool.assert_not_awaited()


def test_csv_retired_source_is_503(client, orch):
    from orchestrator.orchestrator import RETIRED_AGENT_IDS
    retired = next(iter(RETIRED_AGENT_IDS))
    orch.workspace.aget_by_component_id.return_value = _row(
        _paginated_cd() | {"_source_agent": retired})
    r = _csv_get(client)
    assert r.status_code == 503
    assert r.json()["error"] == "source_retired"
    orch.execute_authorized_tool.assert_not_awaited()


def test_csv_export_audited(client, orch, monkeypatch):
    import audit.hooks as hooks
    rec = AsyncMock()
    monkeypatch.setattr(hooks, "record_workspace_event", rec)
    orch.workspace.aget_by_component_id.return_value = _row(_table_cd())
    assert _csv_get(client).status_code == 200
    assert rec.await_count == 1
    kwargs = rec.await_args.kwargs
    assert kwargs["action"] == "component_exported"
    assert kwargs["chat_id"] == CHAT_ID and kwargs["component_id"] == "wc_tbl1"


# ───────────────────────── Canvas HTML export ────────────────────────────────


def _canvas():
    return [
        _table_cd(),
        {"type": "bar_chart", "component_id": "wc_ch", "title": "Chart",
         "labels": ["x", "y"], "datasets": [{"label": "s", "data": [1, 2]}],
         "provenance": "grounded"},
        {"type": "card", "component_id": "wc_card", "title": "Note",
         "content": "hello", "provenance": "generated"},
    ]


def test_canvas_export_standalone_document(client, orch):
    orch._canvas_components.return_value = _canvas()
    r = client.get(f"/api/export/canvas/{CHAT_ID}.html", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-disposition"] == \
        f'attachment; filename="canvas-{CHAT_ID}.html"'
    html = r.text
    assert html.startswith("<!DOCTYPE html>")
    assert "<script" not in html
    # Components arrive under their identities.
    for cid in ("wc_tbl1", "wc_ch", "wc_card"):
        assert f'data-component-id="{cid}"' in html
    # The chart degraded down its fallback ladder — a table, not a live mount.
    assert 'class="astral-chart"' not in html
    assert ">x<" in html and ">y<" in html
    # Provenance + date stamped.
    assert "astral-provenance--grounded" in html
    assert "2 grounded" in html and "1 generated" in html
    assert f"Generated {date.today().isoformat()} by AstralDeep" in html
    orch._canvas_components.assert_called_once_with(CHAT_ID, USER_ID)


def test_canvas_export_empty_or_foreign_chat_is_404(client, orch):
    orch._canvas_components.return_value = []
    r = client.get(f"/api/export/canvas/{CHAT_ID}.html", headers=AUTH)
    assert r.status_code == 404


def test_canvas_export_audited(client, orch, monkeypatch):
    import audit.hooks as hooks
    rec = AsyncMock()
    monkeypatch.setattr(hooks, "record_workspace_event", rec)
    orch._canvas_components.return_value = _canvas()
    assert client.get(f"/api/export/canvas/{CHAT_ID}.html", headers=AUTH).status_code == 200
    assert rec.await_args.kwargs["action"] == "canvas_exported"


# ───────────────────────── Flag + auth gates ─────────────────────────────────


def test_flag_off_both_routes_404_route_absent_body(client, orch):
    orch.workspace.aget_by_component_id.return_value = _row(_table_cd())
    orch._canvas_components.return_value = _canvas()
    flags._flags["artifact_export"] = False
    r1 = _csv_get(client)
    r2 = client.get(f"/api/export/canvas/{CHAT_ID}.html", headers=AUTH)
    assert (r1.status_code, r2.status_code) == (404, 404)
    # Body indistinguishable from an unregistered route.
    assert r1.json() == {"detail": "Not Found"}
    assert r2.json() == {"detail": "Not Found"}


def _no_session(monkeypatch):
    """No astral_session cookie resolves (real deployments without a login)."""
    async def _none(request):
        return None
    monkeypatch.setattr(web_auth, "ensure_session", _none)


def test_unauthenticated_api_requests_are_401(client, orch, monkeypatch):
    _no_session(monkeypatch)
    orch.workspace.aget_by_component_id.return_value = _row(_table_cd())
    headers = {"Accept": "application/json"}
    r1 = client.get(f"/api/export/component/wc_tbl1.csv?chat_id={CHAT_ID}",
                    headers=headers)
    r2 = client.get(f"/api/export/canvas/{CHAT_ID}.html", headers=headers)
    assert (r1.status_code, r2.status_code) == (401, 401)


def test_unauthenticated_browser_navigation_redirects_to_login(client, monkeypatch):
    """Middle-click / system-browser open with no session: 302 to login with
    next= carrying the export path+query, never a 'not authenticated' page."""
    from urllib.parse import quote
    _no_session(monkeypatch)
    accept = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    csv_path = f"/api/export/component/wc_tbl1.csv?chat_id={CHAT_ID}"
    r1 = client.get(csv_path, headers=accept, follow_redirects=False)
    assert r1.status_code == 302
    assert r1.headers["location"] == f"/auth/login?next={quote(csv_path, safe='')}"
    html_path = f"/api/export/canvas/{CHAT_ID}.html"
    r2 = client.get(html_path, headers=accept, follow_redirects=False)
    assert r2.status_code == 302
    assert r2.headers["location"] == f"/auth/login?next={quote(html_path, safe='')}"


def test_cookie_session_mock_mode_serves_export(client, orch):
    """No Authorization header at all — the astral_session cookie path
    (USE_MOCK_AUTH=true makes ensure_session return the test_user session)."""
    orch.workspace.aget_by_component_id.return_value = _row(_table_cd())
    r = client.get("/api/export/component/wc_tbl1.csv", params={"chat_id": CHAT_ID})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    orch.workspace.aget_by_component_id.assert_awaited_once_with(
        CHAT_ID, "test_user", "wc_tbl1")


def test_cookie_session_real_mode_serves_export(client, orch, monkeypatch):
    """Non-mock: the faked session's access token flows through the SAME JWKS
    verification path as a Bearer token (test_download_auth.py pattern)."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")

    async def _sess(request):
        return {"access_token": "signed.jwt.token", "refresh_token": "",
                "sub": USER_ID, "created_at": 0, "resumed": True, "sid": "s"}
    monkeypatch.setattr(web_auth, "ensure_session", _sess)

    async def _jwks(url, token=None):
        return {"keys": [{"kid": "k"}]}
    monkeypatch.setattr("shared.jwks_cache.get_jwks", _jwks)
    monkeypatch.setattr(
        "jose.jwt.decode",
        lambda token, key, **kw: {"sub": USER_ID, "azp": "astral-frontend"},
    )

    orch.workspace.aget_by_component_id.return_value = _row(_table_cd())
    r = client.get("/api/export/component/wc_tbl1.csv", params={"chat_id": CHAT_ID})
    assert r.status_code == 200
    orch.workspace.aget_by_component_id.assert_awaited_once_with(
        CHAT_ID, USER_ID, "wc_tbl1")


def test_bearer_takes_precedence_over_cookie(client, orch, monkeypatch):
    async def _boom(request):
        raise AssertionError("ensure_session must not be called when a Bearer token exists")
    monkeypatch.setattr(web_auth, "ensure_session", _boom)
    orch.workspace.aget_by_component_id.return_value = _row(_table_cd())
    assert _csv_get(client).status_code == 200
