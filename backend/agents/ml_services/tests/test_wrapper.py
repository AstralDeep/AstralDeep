"""Tests for the shared external-service foundation ``_wrapper`` (feature 029, T024).

Covers the retry-classification shim (formerly duplicated across the three
predecessor mcp_servers), per-bundle credential resolution, the unified HTTP
client, and the MCP server's error-branch behavior over the union registry.
"""
import json
import socket
from unittest.mock import patch

import pytest

from agents.ml_services import _wrapper
from agents.ml_services.mcp_server import MCPServer
from shared.protocol import MCPRequest
from shared.tests._http_mock import HttpMock


# ---------------------------------------------------------------------------
# Retry shim
# ---------------------------------------------------------------------------


def test_retryable_exception_types() -> None:
    import requests
    for exc in (
        ConnectionError("x"),
        TimeoutError("x"),
        OSError("x"),
        json.JSONDecodeError("x", "doc", 0),
        requests.exceptions.RequestException("x"),
    ):
        assert _wrapper.is_retryable_error(exc) is True, type(exc).__name__


def test_non_retryable_exception_types() -> None:
    for exc in (TypeError("x"), KeyError("x"), ValueError("x"), AttributeError("x")):
        assert _wrapper.is_retryable_error(exc) is False, type(exc).__name__


def test_unknown_exception_defaults_to_retryable() -> None:
    class WeirdError(Exception):
        pass
    assert _wrapper.is_retryable_error(WeirdError("x")) is True


def test_shim_includes_requests_exceptions() -> None:
    """The requests-tolerant shim must be active in this environment."""
    import requests
    assert requests.exceptions.RequestException in _wrapper.RETRYABLE_EXCEPTIONS


# ---------------------------------------------------------------------------
# Credential bundles + resolution
# ---------------------------------------------------------------------------


def test_bundle_key_names_are_the_existing_ones() -> None:
    assert (_wrapper.CLASSIFY_BUNDLE.url_key, _wrapper.CLASSIFY_BUNDLE.api_key_key) == \
        ("CLASSIFY_URL", "CLASSIFY_API_KEY")
    assert (_wrapper.FORECASTER_BUNDLE.url_key, _wrapper.FORECASTER_BUNDLE.api_key_key) == \
        ("FORECASTER_URL", "FORECASTER_API_KEY")
    assert (_wrapper.LLM_FACTORY_BUNDLE.url_key, _wrapper.LLM_FACTORY_BUNDLE.api_key_key) == \
        ("LLM_FACTORY_URL", "LLM_FACTORY_API_KEY")


def test_bundle_configured() -> None:
    creds = {"CLASSIFY_URL": "https://x.example.com", "CLASSIFY_API_KEY": "k"}
    assert _wrapper.bundle_configured(creds, _wrapper.CLASSIFY_BUNDLE) is True
    assert _wrapper.bundle_configured(creds, _wrapper.FORECASTER_BUNDLE) is False
    assert _wrapper.bundle_configured({"CLASSIFY_URL": "https://x"}, _wrapper.CLASSIFY_BUNDLE) is False
    assert _wrapper.bundle_configured({"CLASSIFY_URL": "", "CLASSIFY_API_KEY": "k"},
                                      _wrapper.CLASSIFY_BUNDLE) is False
    assert _wrapper.bundle_configured({}, _wrapper.CLASSIFY_BUNDLE) is False
    assert _wrapper.bundle_configured(None, _wrapper.CLASSIFY_BUNDLE) is False


def test_build_client_missing_credentials_message() -> None:
    with pytest.raises(ValueError, match="CLASSify is not configured"):
        _wrapper.build_client({}, _wrapper.CLASSIFY_BUNDLE)
    with pytest.raises(ValueError, match="Timeseries Forecaster is not configured"):
        _wrapper.build_client({}, _wrapper.FORECASTER_BUNDLE)
    with pytest.raises(ValueError, match="LLM-Factory is not configured"):
        _wrapper.build_client({}, _wrapper.LLM_FACTORY_BUNDLE)


def test_build_client_stale_credentials_message() -> None:
    with pytest.raises(ValueError, match="could not be decrypted"):
        _wrapper.build_client(
            {"_credentials": {}, "_credentials_stale": True},
            _wrapper.LLM_FACTORY_BUNDLE,
        )


def test_validate_messages_use_service_labels() -> None:
    client = _wrapper.ExternalServiceClient(
        {"CLASSIFY_API_KEY": "k"}, _wrapper.CLASSIFY_BUNDLE)
    with pytest.raises(ValueError, match="CLASSify Service URL is not configured"):
        client.validate()
    client = _wrapper.ExternalServiceClient(
        {"FORECASTER_URL": "https://f.example.com"}, _wrapper.FORECASTER_BUNDLE)
    with pytest.raises(ValueError, match="Forecaster API Key is not configured"):
        client.validate()


def test_llm_factory_client_strips_v1_suffix() -> None:
    client = _wrapper.ExternalServiceClient(
        {"LLM_FACTORY_URL": "https://llm.example.com/v1", "LLM_FACTORY_API_KEY": "k"},
        _wrapper.LLM_FACTORY_BUNDLE,
    )
    assert client.base_url == "https://llm.example.com"


def test_other_bundles_do_not_strip_v1() -> None:
    client = _wrapper.ExternalServiceClient(
        {"CLASSIFY_URL": "https://c.example.com/v1", "CLASSIFY_API_KEY": "k"},
        _wrapper.CLASSIFY_BUNDLE,
    )
    assert client.base_url == "https://c.example.com/v1"


def test_client_get_hits_normalized_url() -> None:
    def _fake_dns(host, *_a, **_kw):
        if host == "c.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        raise socket.gaierror(host)
    with patch("socket.getaddrinfo", _fake_dns), HttpMock() as m:
        m.add("GET", "https://c.example.com/reports/get-ml-opts", status=200, json={})
        client = _wrapper.ExternalServiceClient(
            {"CLASSIFY_URL": "https://c.example.com/", "CLASSIFY_API_KEY": "k"},
            _wrapper.CLASSIFY_BUNDLE,
        )
        resp = client.get("reports/get-ml-opts")  # no leading slash on purpose
        assert resp.status_code == 200
        assert m.calls[-1]["url"] == "https://c.example.com/reports/get-ml-opts"


# ---------------------------------------------------------------------------
# Error mapping helpers
# ---------------------------------------------------------------------------


def test_verdict_for_exception_mapping() -> None:
    from shared.external_http import (
        AuthFailedError, EgressBlockedError, RateLimitedError,
        ServiceUnreachableError,
    )
    assert _wrapper.verdict_for_exception(AuthFailedError("401"))["credential_test"] == "auth_failed"
    for exc in (ServiceUnreachableError("x"), EgressBlockedError("x"), RateLimitedError("x")):
        assert _wrapper.verdict_for_exception(exc)["credential_test"] == "unreachable"
    assert _wrapper.verdict_for_exception(RuntimeError("x"))["credential_test"] == "unexpected"


def test_user_facing_error_mapping() -> None:
    from shared.external_http import AuthFailedError, BadRequestError, ServiceUnreachableError
    assert _wrapper.user_facing_error(AuthFailedError("401"), "CLASSify") == (
        "The saved CLASSify API key was rejected. Update it in the agent's settings."
    )
    assert _wrapper.user_facing_error(ServiceUnreachableError("down"), "Forecaster") == (
        "Forecaster is unreachable. Try again later."
    )
    assert "rejected the request" in _wrapper.user_facing_error(
        BadRequestError("400"), "LLM-Factory")


def test_ui_serializes_primitives_and_passes_dicts() -> None:
    from astralprims import Text
    out = _wrapper.ui([Text(content="hi"), {"type": "alert", "variant": "info"}],
                      data={"k": 1}, retryable=False)
    assert out["_data"] == {"k": 1}
    assert out["_retryable"] is False
    assert out["_ui_components"][0]["type"] == "text"
    assert out["_ui_components"][1] == {"type": "alert", "variant": "info"}


def test_render_metric_value_shapes() -> None:
    assert _wrapper.render_metric_value(True) == "True"
    assert _wrapper.render_metric_value(0.85) == "0.8500"
    assert _wrapper.render_metric_value(12345.0) == "1.234e+04"
    assert _wrapper.render_metric_value([0.5, False]) == "0.5000, False"
    assert _wrapper.render_metric_value({"a": 1}) == '{"a": 1}'
    assert _wrapper.render_metric_value("x") == "x"


# ---------------------------------------------------------------------------
# MCP server over the union registry (retry behavior, _retryable honoring)
# ---------------------------------------------------------------------------


def _call(server: MCPServer, name: str, arguments=None) -> "object":
    return server.process_request(MCPRequest(
        request_id="req-1", method="tools/call",
        params={"name": name, "arguments": arguments or {}},
    ))


def test_server_unknown_tool_not_retryable() -> None:
    server = MCPServer()
    resp = _call(server, "submit_dataset")  # bare collision verb gone post-029
    assert resp.error is not None
    assert resp.error["code"] == -32601
    assert resp.error["retryable"] is False


def test_server_unknown_method_not_retryable() -> None:
    server = MCPServer()
    resp = server.process_request(MCPRequest(
        request_id="req-1", method="tools/nope", params={}))
    assert resp.error["code"] == -32601
    assert resp.error["retryable"] is False


def test_server_missing_required_argument_rejected_with_hint() -> None:
    server = MCPServer()
    resp = _call(server, "classify_get_results", {})
    assert resp.error is not None
    assert resp.error["code"] == -32602
    assert "report_uuid" in resp.error["message"]
    assert resp.error["retryable"] is False


def test_server_honors_tool_retryable_false_on_error_components() -> None:
    """Tools that return a variant=error Alert with _retryable=False must not
    be auto-retried (the classify/forecaster servers' behavior, now uniform)."""
    server = MCPServer()
    server.tools = {
        "fake_tool": {
            "function": lambda **kw: {
                "_ui_components": [{"type": "alert", "variant": "error", "message": "boom"}],
                "_data": None,
                "_retryable": False,
            },
            "description": "fake",
            "input_schema": {"type": "object", "properties": {}},
            "scope": "tools:read",
        },
    }
    resp = _call(server, "fake_tool")
    assert resp.error["code"] == -32603
    assert resp.error["message"] == "boom"
    assert resp.error["retryable"] is False
    assert resp.ui_components is None


def test_server_classifies_raised_value_error_as_non_retryable() -> None:
    server = MCPServer()
    def _boom(**kw):
        raise ValueError("bad input shape")
    server.tools = {
        "fake_tool": {
            "function": _boom,
            "description": "fake",
            "input_schema": {"type": "object", "properties": {}},
            "scope": "tools:read",
        },
    }
    resp = _call(server, "fake_tool")
    assert resp.error["code"] == -32603
    assert resp.error["retryable"] is False


def test_server_classifies_raised_connection_error_as_retryable() -> None:
    server = MCPServer()
    def _boom(**kw):
        raise ConnectionError("transient")
    server.tools = {
        "fake_tool": {
            "function": _boom,
            "description": "fake",
            "input_schema": {"type": "object", "properties": {}},
            "scope": "tools:read",
        },
    }
    resp = _call(server, "fake_tool")
    assert resp.error["code"] == -32603
    assert resp.error["retryable"] is True


def test_server_unwraps_ui_components_and_data() -> None:
    server = MCPServer()
    server.tools = {
        "fake_tool": {
            "function": lambda **kw: {
                "_ui_components": [{"type": "card", "title": "ok"}],
                "_data": {"value": 7},
                "_retryable": True,
            },
            "description": "fake",
            "input_schema": {"type": "object", "properties": {}},
            "scope": "tools:read",
        },
    }
    resp = _call(server, "fake_tool")
    assert resp.error is None
    assert resp.result == {"value": 7}
    assert resp.ui_components == [{"type": "card", "title": "ok"}]


def test_server_filters_unknown_kwargs_for_non_var_keyword_tools() -> None:
    """Tools without **kwargs only receive declared parameters (ported server
    behavior — protects simple tools from orchestrator-injected kwargs)."""
    server = MCPServer()
    seen = {}
    def _strict(a=None):
        seen["a"] = a
        return {"ok": True}
    server.tools = {
        "fake_tool": {
            "function": _strict,
            "description": "fake",
            "input_schema": {"type": "object", "properties": {}},
            "scope": "tools:read",
        },
    }
    resp = _call(server, "fake_tool", {"a": 1, "_credentials": {"X": "y"}})
    assert resp.error is None
    assert seen == {"a": 1}
