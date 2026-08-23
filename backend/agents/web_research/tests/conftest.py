"""Shared fixtures for the Web Research agent test suite.

Mirrors the forecaster/classify test pattern: ``HttpMock`` stubs the single
``requests.request`` call site used by ``shared.external_http``; DNS is
stubbed so the SSRF guard resolves the test hosts deterministically. All LLM
calls are stubbed — no network anywhere.
"""
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.web_research import mcp_tools
from shared.tests._http_mock import HttpMock

# Hosts that resolve to a public address in tests.
SAFE_HOSTS = {
    "html.duckduckgo.com",
    "lite.duckduckgo.com",
    "search.example.com",
    "example.com",
    "direct.example.org",
    "redirect.example.com",
}
# Hosts that resolve into a private range (egress must be refused).
PRIVATE_HOSTS = {"internal.example.com": "10.0.0.5"}


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
    """Build a fake OpenAI client class.

    Successive ``chat.completions.create`` calls return the strings in
    ``contents`` in order (the last repeats). A content entry that is an
    Exception instance is raised instead. Constructor kwargs and create()
    kwargs are recorded on the class for assertions.
    """
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


class ExplodingOpenAI:
    """Sentinel client class: constructing it means an unwanted LLM call."""

    def __init__(self, **_kwargs):
        raise AssertionError("The LLM client must not be constructed in this test")


@pytest.fixture
def fake_openai(monkeypatch):
    """Install a fake OpenAI class on the tools module; returns the class.

    Feature 054: the OPENAI_* env fallback is gone — LLM credentials reach a
    tool ONLY via the orchestrator-forwarded ``_session_llm_credentials``
    kwarg (or the agent's decrypted ``_credentials`` bundle). Mimic the
    orchestrator here: when a test passes neither kwarg, resolution sees the
    turn's session credentials, exactly as a configured user's call would.
    Tests that pass their own credential kwargs are left untouched.
    """
    def _install(*contents):
        fake_cls = make_fake_openai(list(contents))
        monkeypatch.setattr(mcp_tools, "OpenAI", fake_cls)
        real_resolve = mcp_tools._resolve_llm_client

        def _with_session_creds(kwargs):
            has_session = bool((kwargs.get("_session_llm_credentials") or {}).get("OPENAI_API_KEY"))
            bundle = kwargs.get("_credentials") or {}
            has_bundle_key = bool(bundle.get("OPENAI_API_KEY"))
            # An encrypted bundle key is opaque here (the real resolver
            # ignores it); do NOT paper over that case with session creds.
            if not has_session and not has_bundle_key and not kwargs.get("_credentials_encrypted"):
                kwargs = dict(kwargs)
                kwargs["_session_llm_credentials"] = {"OPENAI_API_KEY": "test-key"}
            return real_resolve(kwargs)

        monkeypatch.setattr(mcp_tools, "_resolve_llm_client", _with_session_creds)
        return fake_cls
    return _install


@pytest.fixture
def no_llm_credentials(monkeypatch):
    """Remove every ambient LLM credential so resolution yields no client."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(mcp_tools, "OpenAI", ExplodingOpenAI)
