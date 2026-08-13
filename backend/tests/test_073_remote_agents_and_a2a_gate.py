"""Feature 073: TLS remote agents, and the ungated /a2a surface.

Covers three defects:

* ``discover_agent`` string-stripped the scheme and always dialled ``ws://``,
  so an https:// agent behind a TLS-terminating proxy was unreachable.
* ``POST /a2a`` was mounted unconditionally, unauthenticated, and dispatched
  through ``execute_tool_and_wait``, which skips ``_authorize_and_prepare``.
* ``/api/tasks/{chat_id}`` and the three ``/api/async-tasks`` routes had no auth
  dependency and no ownership check, and ``list_async_tasks`` bound an
  un-awaited coroutine to ``user_id``.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ["USE_MOCK_AUTH"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.api import async_task_router, task_router  # noqa: E402

OWNER = "owner-user-id"
STRANGER = "stranger-user-id"


def _token(sub: str) -> str:
    import base64
    import json

    body = base64.b64encode(json.dumps({
        "sub": sub,
        "preferred_username": sub,
        "realm_access": {"roles": ["user"]},
    }).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _auth(sub: str) -> dict:
    return {"Authorization": f"Bearer {_token(sub)}"}


# ---------------------------------------------------------------- ws url


@pytest.mark.parametrize("base_url,expected", [
    ("https://panatlas.net", "wss://panatlas.net/agent"),
    ("http://localhost:8003", "ws://localhost:8003/agent"),
    ("http://host.docker.internal:8799", "ws://host.docker.internal:8799/agent"),
    # A trailing slash must not produce a doubled separator.
    ("https://panatlas.net/", "wss://panatlas.net/agent"),
    # A path-prefixed mount is preserved.
    ("https://example.org/agents/panatlas", "wss://example.org/agents/panatlas/agent"),
    # Scheme-less, normalized before parsing so the port is not read as a host.
    ("localhost:8003", "ws://localhost:8003/agent"),
])
def test_agent_ws_url_derives_scheme(base_url, expected):
    from orchestrator.agent_peer_auth import agent_ws_url

    assert agent_ws_url(base_url) == expected


@pytest.mark.parametrize("base_url,expected", [
    ("wss://example.org", "wss://example.org/agent"),
    ("ws://example.org", "ws://example.org/agent"),
])
def test_websocket_scheme_passes_through_without_downgrade(base_url, expected):
    """An operator writing a remote agent in as wss:// must not be downgraded.

    A2A_EXTERNAL_AGENTS names WebSocket agents on other hosts, so spelling one
    `wss://` is a natural mistake; mapping it to `ws` would defeat the TLS this
    helper exists to preserve, and would put the shared agent key in the clear.
    """
    from orchestrator.agent_peer_auth import agent_ws_url

    assert agent_ws_url(base_url) == expected


def test_https_agent_is_not_dialled_over_plaintext():
    """The regression this fixes: https:// must never yield ws://."""
    from orchestrator.agent_peer_auth import agent_ws_url

    url = agent_ws_url("https://panatlas.net")
    assert url.startswith("wss://")
    assert "ws://panatlas" not in url


# ----------------------------------------------------------------- a2a gate


def test_a2a_server_flag_defaults_off():
    """Fail closed: the unauthenticated mount is absent unless opted in."""
    from shared.feature_flags import FeatureFlags

    assert FeatureFlags().is_enabled("a2a_server") is False


def test_a2a_server_flag_reads_env(monkeypatch):
    from shared.feature_flags import FeatureFlags

    monkeypatch.setenv("FF_A2A_SERVER", "true")
    assert FeatureFlags().is_enabled("a2a_server") is True


# --------------------------------------------------------------- task routes


def _task(user_id: str, chat_id: str = "chat-1"):
    t = MagicMock()
    t.user_id = user_id
    t.updated_at = datetime.now(timezone.utc)
    t.to_dict.return_value = {"state": "running", "chat_id": chat_id, "user_id": user_id}
    return t


def _bg_task(user_id: str, task_id: str = "task-1"):
    t = MagicMock()
    t.user_id = user_id
    t.task_id = task_id
    t.chat_id = "chat-1"
    t.status = MagicMock(value="running")
    t.created_at = datetime.now(timezone.utc)
    t.completed_at = None
    t.outputs = []
    t.errors = []
    return t


def _app(orch: MagicMock) -> TestClient:
    app = FastAPI()
    app.state.orchestrator = orch
    app.include_router(task_router)
    app.include_router(async_task_router)
    return TestClient(app)


@pytest.fixture
def orch() -> MagicMock:
    o = MagicMock()
    o.task_manager.get_active_task.return_value = _task(OWNER)
    o.task_manager.get_chat_tasks.return_value = [_task(OWNER)]
    o.async_task_manager.get = AsyncMock(return_value=_bg_task(OWNER))
    o.async_task_manager.list_for_user = AsyncMock(return_value=[_bg_task(OWNER)])
    o.async_task_manager.cancel = AsyncMock(return_value=True)
    return o


@pytest.mark.parametrize("method,path", [
    ("get", "/api/tasks/chat-1"),
    ("get", "/api/async-tasks/task-1"),
    ("get", "/api/async-tasks"),
    ("post", "/api/async-tasks/task-1/cancel"),
])
def test_routes_require_authentication(orch, method, path):
    """All four were reachable with no credentials at all."""
    resp = getattr(_app(orch), method)(path)
    assert resp.status_code == 401


def test_task_state_returns_owner_task(orch):
    resp = _app(orch).get("/api/tasks/chat-1", headers=_auth(OWNER))
    assert resp.status_code == 200
    assert resp.json()["state"] == "running"


def test_task_state_hides_another_users_task(orch):
    """chat_id possession must not disclose another user's task state."""
    resp = _app(orch).get("/api/tasks/chat-1", headers=_auth(STRANGER))
    assert resp.status_code == 200
    assert resp.json() == {"state": "none", "chat_id": "chat-1"}


def test_async_task_visible_to_owner(orch):
    resp = _app(orch).get("/api/async-tasks/task-1", headers=_auth(OWNER))
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "task-1"


def test_async_task_hidden_from_stranger(orch):
    """Non-owned and unknown share one non-disclosing response."""
    resp = _app(orch).get("/api/async-tasks/task-1", headers=_auth(STRANGER))
    assert resp.status_code == 404

    orch.async_task_manager.get = AsyncMock(return_value=None)
    unknown = _app(orch).get("/api/async-tasks/nope", headers=_auth(STRANGER))
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "Task not found"


def test_list_async_tasks_filters_by_resolved_user(orch):
    """Regression: user_id used to be an un-awaited coroutine object."""
    resp = _app(orch).get("/api/async-tasks", headers=_auth(OWNER))
    assert resp.status_code == 200
    assert len(resp.json()["tasks"]) == 1
    orch.async_task_manager.list_for_user.assert_awaited_once()
    passed_user = orch.async_task_manager.list_for_user.await_args.args[0]
    assert passed_user == OWNER
    assert isinstance(passed_user, str)


def test_cancel_requires_ownership(orch):
    resp = _app(orch).post("/api/async-tasks/task-1/cancel", headers=_auth(STRANGER))
    assert resp.status_code == 404
    # The ownership check must run BEFORE the cancel, not after.
    orch.async_task_manager.cancel.assert_not_awaited()


def test_cancel_succeeds_for_owner(orch):
    resp = _app(orch).post("/api/async-tasks/task-1/cancel", headers=_auth(OWNER))
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    orch.async_task_manager.cancel.assert_awaited_once()


def test_cancel_of_finished_task_reports_not_found(orch):
    orch.async_task_manager.cancel = AsyncMock(return_value=False)
    resp = _app(orch).post("/api/async-tasks/task-1/cancel", headers=_auth(OWNER))
    assert resp.status_code == 404
