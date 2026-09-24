"""Shared pytest fixtures for the Summarizer test suite: stubs requests via
backend/shared/tests/_http_mock.py, stubs DNS for SSRF-gate determinism, and fakes
the OpenAI client.
"""

import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.summarizer import mcp_tools
from shared.tests._http_mock import HttpMock

SAFE_HOSTS = {"example.com", "redirect.example.com"}
PRIVATE_HOSTS = {"internal.example.com": "192.168.1.20"}


@pytest.fixture
def rmock():
    with HttpMock() as m:
        yield m


@pytest.fixture(autouse=True)
def stub_dns():
    def _fake(host, *_a, **_kw):
        if host in SAFE_HOSTS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        if host in PRIVATE_HOSTS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (PRIVATE_HOSTS[host], 0))]
        raise socket.gaierror(host)
    with patch("socket.getaddrinfo", _fake):
        yield


def make_fake_openai(contents):
    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            content = contents[min(len(calls) - 1, len(contents) - 1)]
            if isinstance(content, Exception):
                raise content
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    class _FakeClient:
        calls_log = calls
        last_init = {}

        def __init__(self, api_key=None, base_url=None):
            _FakeClient.last_init = {"api_key": api_key, "base_url": base_url}
            self.chat = SimpleNamespace(completions=_Completions())

    return _FakeClient


@pytest.fixture
def fake_openai(monkeypatch):
    def _install(*contents):
        fake_cls = make_fake_openai(list(contents))
        monkeypatch.setattr(mcp_tools, "OpenAI", fake_cls)
        real_resolve = mcp_tools._resolve_llm_client

        def _with_session_creds(kwargs):
            has_session = bool((kwargs.get("_session_llm_credentials") or {}).get("OPENAI_API_KEY"))
            bundle = kwargs.get("_credentials") or {}
            has_bundle_key = bool(bundle.get("OPENAI_API_KEY"))
            if not has_session and not has_bundle_key and not kwargs.get("_credentials_encrypted"):
                kwargs = dict(kwargs)
                kwargs["_session_llm_credentials"] = {"OPENAI_API_KEY": "test-key"}
            return real_resolve(kwargs)

        monkeypatch.setattr(mcp_tools, "_resolve_llm_client", _with_session_creds)
        return fake_cls
    return _install


@pytest.fixture
def no_llm_credentials(monkeypatch):
    class _ExplodingOpenAI:
        def __init__(self, **_kwargs):
            raise AssertionError("The LLM client must not be constructed here")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(mcp_tools, "OpenAI", _ExplodingOpenAI)
