from __future__ import annotations

import os
import pytest

from backend.llm.providers.anthropic_native_adapter import AnthropicNativeClient
from backend.llm_config.client_factory import create_client


def test_factory_selects_native_by_explicit_profile():
    os.environ["ANTHROPIC_CUSTOM_HEADERS"] = "injected"
    client = create_client(
        owner_id="owner-1",
        model_name="claude-3-5-sonnet-20241022",
        profile_override="anthropic_native",
    )
    assert isinstance(client, AnthropicNativeClient)
    del os.environ["ANTHROPIC_CUSTOM_HEADERS"]


def test_factory_fails_closed_missing_credentials():
    with pytest.raises(ValueError):
        create_client(owner_id="owner-no-creds", model_name="x", profile_override="anthropic_native")


def test_usage_normalization_no_double_count():
    client = AnthropicNativeClient(owner_id="o", endpoint="https://api.anthropic.com/v1/messages", api_key="k")
    usage = client._normalize_usage({
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_creation_input_tokens": 50,
        "cache_read_input_tokens": 300,
    })
    assert usage.input_tokens == 700
    assert usage.cache_write_tokens == 50
    assert usage.cache_read_tokens == 300
    assert usage.total_tokens == 700 + 50 + 300 + 200


def test_headers_deterministic_no_ambient():
    os.environ["ANTHROPIC_CUSTOM_HEADERS"] = '{"x-evil":"1"}'
    client = AnthropicNativeClient(owner_id="o", endpoint="https://api.anthropic.com/v1/messages", api_key="secret")
    h = client._headers()
    assert set(h.keys()) == {"x-api-key", "anthropic-version", "content-type"}
    assert h["x-api-key"] == "secret"
    del os.environ["ANTHROPIC_CUSTOM_HEADERS"]


def test_endpoint_validation():
    with pytest.raises(ValueError):
        AnthropicNativeClient(owner_id="o", endpoint="http://evil.com", api_key="k")
    client = AnthropicNativeClient(owner_id="o", endpoint="https://api.anthropic.com/v1/messages", api_key="k")
    assert client.endpoint == "https://api.anthropic.com/v1/messages"
