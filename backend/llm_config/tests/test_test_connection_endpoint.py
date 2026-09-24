"""Tests for llm_config/api.py's POST /api/llm/test endpoint: field validation,
success/failure error_class classification, audit emission that never carries the API
key, and the per-user probe rate limit (429).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import llm_config.api as llm_api
from llm_config.api import llm_router


# Rate limiter is module-global state — reset between tests
@pytest.fixture(autouse=True)
def _reset_probe_rate_state():
    llm_api._probe_hits.clear()
    yield
    llm_api._probe_hits.clear()


@pytest.fixture
def fake_recorder():
    rec = MagicMock()
    rec.record = AsyncMock()
    return rec


@pytest.fixture
def app(fake_recorder):
    app = FastAPI()
    app.include_router(llm_router)
    app.state.orchestrator = SimpleNamespace(audit_recorder=fake_recorder)
    from orchestrator.auth import require_user_id, get_current_user_payload
    app.dependency_overrides[require_user_id] = lambda: "test_user"
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": "test_user",
        "preferred_username": "test_user",
    }
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _success_response():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok"),
                index=0,
                finish_reason="length",
            )
        ],
        usage=SimpleNamespace(total_tokens=1, prompt_tokens=1, completion_tokens=0),
    )


def test_missing_field_returns_422(client):
    r = client.post("/api/llm/test", json={"api_key": "x", "base_url": "https://x/v1"})
    assert r.status_code == 422


def test_non_http_base_url_returns_422(client):
    r = client.post("/api/llm/test", json={
        "api_key": "x", "base_url": "ftp://x/v1", "model": "m",
    })
    assert r.status_code == 422


def test_empty_after_trim_returns_422(client):
    r = client.post("/api/llm/test", json={
        "api_key": "  ", "base_url": "https://x/v1", "model": "m",
    })
    assert r.status_code == 422


def test_success_returns_ok_true(client, fake_recorder):
    fake = MagicMock()
    fake.chat.completions.create = MagicMock(return_value=_success_response())
    with patch("llm_config.api.OpenAI", return_value=fake):
        r = client.post("/api/llm/test", json={
            "api_key": "sk-realkey1234567890abcd",
            "base_url": "https://x.example/v1",
            "model": "model-a",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["model"] == "model-a"
    assert body["error_class"] is None
    assert body["latency_ms"] is not None
    assert fake_recorder.record.await_count == 1
    ev = fake_recorder.record.await_args.args[0]
    assert ev.event_class == "llm_config_change"
    assert ev.inputs_meta["action"] == "tested"
    assert ev.outputs_meta["result"] == "success"
    serialised = ev.model_dump_json()
    assert "sk-realkey1234567890abcd" not in serialised


def test_probe_is_transient_and_cannot_replace_saved_runtime_config(
    client,
    app,
    store,
    fake_db,
):
    store.set_sync(
        "test_user",
        provider="custom",
        base_url="https://saved.example/v1",
        model="saved-model",
        api_key="saved-test-key",
    )
    app.state.orchestrator._llm_store = store
    fake = MagicMock()
    fake.chat.completions.create = MagicMock(return_value=_success_response())

    with patch("llm_config.api.OpenAI", return_value=fake):
        response = client.post(
            "/api/llm/test",
            json={
                "api_key": "prospective-test-key",
                "base_url": "https://prospective.example/v1",
                "model": "prospective-model",
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["model"] == "prospective-model"
    assert store.get_sync("test_user").model == "saved-model"
    assert fake_db.users["test_user"]["model"] == "saved-model"


@pytest.mark.parametrize("exc_message,expected_class", [
    ("Incorrect API key provided. (HTTP 401)", "auth_failed"),
    ("model 'gpt-9' does not exist (HTTP 404)", "model_not_found"),
    ("Connection refused", "transport_error"),
    ("Read timeout", "transport_error"),
    ("response missing 'choices' — not OpenAI-compatible", "contract_violation"),
])
def test_failure_classifies_error_class(client, fake_recorder, exc_message, expected_class):
    fake = MagicMock()
    fake.chat.completions.create = MagicMock(side_effect=Exception(exc_message))
    with patch("llm_config.api.OpenAI", return_value=fake):
        r = client.post("/api/llm/test", json={
            "api_key": "sk-x1234567890abcdefghij",
            "base_url": "https://x.example/v1",
            "model": "m",
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == expected_class
    assert body["upstream_message"] == exc_message
    ev = fake_recorder.record.await_args.args[0]
    assert ev.outputs_meta["result"] == "failure"
    assert ev.outputs_meta["error_class"] == expected_class


def test_contract_violation_when_choices_missing(client, fake_recorder):
    fake = MagicMock()
    bogus = SimpleNamespace(choices=None)
    fake.chat.completions.create = MagicMock(return_value=bogus)
    with patch("llm_config.api.OpenAI", return_value=fake):
        r = client.post("/api/llm/test", json={
            "api_key": "sk-x1234567890abcdefghij",
            "base_url": "https://x.example/v1",
            "model": "m",
        })
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "contract_violation"


def test_failure_audit_does_not_contain_api_key(client, fake_recorder):
    fake = MagicMock()
    fake.chat.completions.create = MagicMock(side_effect=Exception("arbitrary error"))
    with patch("llm_config.api.OpenAI", return_value=fake):
        r = client.post("/api/llm/test", json={
            "api_key": "sk-mostsensitive1234567890abcdef",
            "base_url": "https://x.example/v1",
            "model": "m",
        })
    assert r.status_code == 200
    ev = fake_recorder.record.await_args.args[0]
    serialised = ev.model_dump_json()
    assert "sk-mostsensitive1234567890abcdef" not in serialised


_PROBE_BODY = {
    "api_key": "sk-ratelimit1234567890abcd",
    "base_url": "https://x.example/v1",
    "model": "m",
}


def test_probe_rate_limit_429_on_third_call(client, fake_recorder, monkeypatch):
    monkeypatch.setattr(llm_api, "_PROBE_RATE_PER_MINUTE", 2)
    llm_api._probe_hits.clear()
    fake = MagicMock()
    fake.chat.completions.create = MagicMock(return_value=_success_response())
    with patch("llm_config.api.OpenAI", return_value=fake):
        assert client.post("/api/llm/test", json=_PROBE_BODY).status_code == 200
        assert client.post("/api/llm/test", json=_PROBE_BODY).status_code == 200
        r3 = client.post("/api/llm/test", json=_PROBE_BODY)
    assert r3.status_code == 429
    assert "probe" in r3.json()["detail"].lower()
    assert fake.chat.completions.create.call_count == 2
    assert fake_recorder.record.await_count == 2


def test_probe_rate_limit_shared_with_list_models(client, monkeypatch):
    monkeypatch.setattr(llm_api, "_PROBE_RATE_PER_MINUTE", 2)
    llm_api._probe_hits.clear()
    fake = MagicMock()
    fake.chat.completions.create = MagicMock(return_value=_success_response())
    fake.models.list = MagicMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(id="m")]))
    with patch("llm_config.api.OpenAI", return_value=fake):
        assert client.post("/api/llm/test", json=_PROBE_BODY).status_code == 200
        assert client.post("/api/llm/test", json=_PROBE_BODY).status_code == 200
        r = client.post("/api/llm/list-models", json={
            "api_key": _PROBE_BODY["api_key"],
            "base_url": _PROBE_BODY["base_url"],
        })
    assert r.status_code == 429


def test_probe_rate_limit_window_expires(client, monkeypatch):
    monkeypatch.setattr(llm_api, "_PROBE_RATE_PER_MINUTE", 1)
    llm_api._probe_hits.clear()
    fake = MagicMock()
    fake.chat.completions.create = MagicMock(return_value=_success_response())
    with patch("llm_config.api.OpenAI", return_value=fake):
        assert client.post("/api/llm/test", json=_PROBE_BODY).status_code == 200
        assert client.post("/api/llm/test", json=_PROBE_BODY).status_code == 429
        hits = llm_api._probe_hits["test_user"]
        hits[0] -= 61.0
        assert client.post("/api/llm/test", json=_PROBE_BODY).status_code == 200
