"""Tests for TLS remote-agent dialing (agent_peer_auth.py) and the /a2a and
task/async-task routes in orchestrator/api.py: scheme derivation, the A2A feature
flag gate, and per-owner auth on task state.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


OWNER = "owner-user-id"
STRANGER = "stranger-user-id"


_identity = None


def _auth(sub: str) -> dict:
    return _identity.headers(sub)


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


def test_a2a_server_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("FF_A2A_SERVER", raising=False)
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


