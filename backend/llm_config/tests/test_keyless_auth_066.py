"""Feature-066 regression pins for keyless OpenAI-compatible endpoints.

Keyless servers (vLLM/sglang, local runtimes, the UK LLM factory) accept a
MISSING Authorization header while rejecting an arbitrary wrong bearer with
401/403 — so the SDK's ``Bearer not-needed`` placeholder must never reach the
wire. ``openai_auth_kwargs`` selects either a real-key pass-through or the
shared keyless transport whose request hook strips the header.
"""

from __future__ import annotations

import httpx

from llm_config.client_factory import (
    KEYLESS_API_KEY_SENTINEL,
    openai_auth_kwargs,
)
from llm_config.providers import all_presets, get_preset


def test_real_key_passes_through_untouched() -> None:
    assert openai_auth_kwargs("sk-real-key") == {"api_key": "sk-real-key"}


def test_empty_key_selects_the_keyless_transport() -> None:
    kwargs = openai_auth_kwargs("")
    assert kwargs["api_key"] == KEYLESS_API_KEY_SENTINEL
    assert "http_client" in kwargs


def test_sentinel_key_is_treated_as_keyless() -> None:
    kwargs = openai_auth_kwargs(KEYLESS_API_KEY_SENTINEL)
    assert "http_client" in kwargs


def test_keyless_http_client_is_shared() -> None:
    a = openai_auth_kwargs("")["http_client"]
    b = openai_auth_kwargs("")["http_client"]
    assert a is b


def test_keyless_hook_strips_the_authorization_header() -> None:
    hooks = openai_auth_kwargs("")["http_client"].event_hooks["request"]
    assert hooks, "keyless client must carry a request hook"
    request = httpx.Request(
        "POST",
        "https://speech.example.test/v1/chat/completions",
        headers={"Authorization": "Bearer not-needed"},
    )
    for hook in hooks:
        hook(request)
    assert "Authorization" not in request.headers


def test_no_authorization_header_reaches_the_wire_keyless() -> None:
    """End-to-end through httpx: the hook runs before the transport."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    hooks = openai_auth_kwargs("")["http_client"].event_hooks["request"]
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": list(hooks)},
    )
    client.post(
        "https://speech.example.test/v1/models",
        headers={"Authorization": "Bearer not-needed"},
    )
    assert seen["auth"] is None


def test_custom_preset_is_key_optional() -> None:
    assert get_preset("custom").key_required is False
    keyless = {p.key for p in all_presets() if not p.key_required}
    assert keyless == {"ollama", "lmstudio", "custom"}
