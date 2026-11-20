"""Tests for llm_config/deadlines.py and the client timeout built from it."""

import types

import pytest

from llm_config.client_factory import build_llm_client
from llm_config.deadlines import LOCAL, REMOTE, LLMDeadlines, deadlines_for, stream_wait
from llm_config.types import CredentialSource

ENV = ("LLM_CONNECT_TIMEOUT_SECONDS", "LLM_FIRST_TOKEN_TIMEOUT_SECONDS",
       "LLM_IDLE_TIMEOUT_SECONDS", "LLM_ATTEMPT_TIMEOUT_SECONDS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_remote_and_local_defaults():
    assert deadlines_for("https://api.example.com/v1") == LLMDeadlines(10.0, 60.0, 120.0, 600.0)
    assert deadlines_for("http://127.0.0.1:11434/v1") == LLMDeadlines(10.0, 180.0, 300.0, 600.0)
    assert deadlines_for("http://192.168.1.5:8000/v1") == LOCAL
    assert deadlines_for(None) == REMOTE


@pytest.mark.parametrize("raw, expected", [("5", 5.0), ("0", 1.0), ("99999", 3600.0), ("abc", 120.0), ("", 120.0),
                                           ("nan", 120.0)])
def test_env_override_is_clamped_or_ignored(monkeypatch, raw, expected):
    monkeypatch.setenv("LLM_IDLE_TIMEOUT_SECONDS", raw)
    assert deadlines_for("https://api.example.com/v1").idle == expected


def test_every_override_applies_to_its_own_field(monkeypatch):
    for index, name in enumerate(ENV):
        monkeypatch.setenv(name, str(index + 2))
    assert deadlines_for("https://api.example.com/v1") == LLMDeadlines(2.0, 3.0, 4.0, 5.0)


def test_http_timeout_shape():
    timeout = REMOTE.http_timeout()
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (10.0, 120.0, 30.0, 10.0)


def test_stream_wait_picks_the_nearest_deadline():
    assert stream_wait(REMOTE, now=1.0, started=0.0, last_progress=0.0, progressed=False) == (59.0, "first_token")
    assert stream_wait(REMOTE, now=100.0, started=0.0, last_progress=90.0, progressed=True) == (110.0, "idle")
    assert stream_wait(REMOTE, now=595.0, started=0.0, last_progress=594.0, progressed=True) == (5.0, "attempt")
    assert stream_wait(REMOTE, now=700.0, started=0.0, last_progress=699.0, progressed=True) == (0.0, "attempt")


def test_build_llm_client_uses_the_profile_unless_a_timeout_is_given():
    cfg = types.SimpleNamespace(api_key="k", base_url="https://api.example.com/v1", model="m")
    client, _, _ = build_llm_client(cfg, CredentialSource.USER)
    assert client.timeout == REMOTE.http_timeout()
    assert client.max_retries == 0
    explicit, _, _ = build_llm_client(cfg, CredentialSource.USER, timeout=10.0)
    assert explicit.timeout == 10.0
