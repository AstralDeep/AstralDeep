"""Tests for TLS remote-agent dialing (agent_peer_auth.py) and the /a2a and
task/async-task routes in orchestrator/api.py: scheme derivation, the A2A feature
flag gate, and per-owner auth on task state.
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


@pytest.mark.parametrize("base_url,expected", [
    ("https://panatlas.net", "wss://panatlas.net/agent"),
    ("http://localhost:8003", "ws://localhost:8003/agent"),
    ("http://host.docker.internal:8799", "ws://host.docker.internal:8799/agent"),
    ("https://panatlas.net/", "wss://panatlas.net/agent"),
    ("https://example.org/agents/panatlas", "wss://example.org/agents/panatlas/agent"),
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
    from orchestrator.agent_peer_auth import agent_ws_url

    assert agent_ws_url(base_url) == expected


def test_https_agent_is_not_dialled_over_plaintext():
    from orchestrator.agent_peer_auth import agent_ws_url

    url = agent_ws_url("https://panatlas.net")
    assert url.startswith("wss://")
    assert "ws://panatlas" not in url


def test_a2a_server_flag_defaults_off():
    from shared.feature_flags import FeatureFlags

    assert FeatureFlags().is_enabled("a2a_server") is False


def test_a2a_server_flag_reads_env(monkeypatch):
    from shared.feature_flags import FeatureFlags

    monkeypatch.setenv("FF_A2A_SERVER", "true")
    assert FeatureFlags().is_enabled("a2a_server") is True


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
    resp = getattr(_app(orch), method)(path)
    assert resp.status_code == 401


def test_task_state_returns_owner_task(orch):
    resp = _app(orch).get("/api/tasks/chat-1", headers=_auth(OWNER))
    assert resp.status_code == 200
    assert resp.json()["state"] == "running"


def test_task_state_hides_another_users_task(orch):
    resp = _app(orch).get("/api/tasks/chat-1", headers=_auth(STRANGER))
    assert resp.status_code == 200
    assert resp.json() == {"state": "none", "chat_id": "chat-1"}


def test_async_task_visible_to_owner(orch):
    resp = _app(orch).get("/api/async-tasks/task-1", headers=_auth(OWNER))
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "task-1"


def test_async_task_hidden_from_stranger(orch):
    resp = _app(orch).get("/api/async-tasks/task-1", headers=_auth(STRANGER))
    assert resp.status_code == 404

    orch.async_task_manager.get = AsyncMock(return_value=None)
    unknown = _app(orch).get("/api/async-tasks/nope", headers=_auth(STRANGER))
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "Task not found"


def test_list_async_tasks_filters_by_resolved_user(orch):
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
