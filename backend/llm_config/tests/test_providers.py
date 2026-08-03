"""Feature 054 — provider preset catalog tests (spec FR-011/FR-012).

The catalog is the single server-owned source of truth for the setup
dialog's dropdown; base URLs for presets are server-derived at save time
(submitted values ignored), and only ``custom`` honors a caller URL.
"""
from __future__ import annotations

from urllib.parse import urlparse

from llm_config.providers import (
    CUSTOM_PROVIDER_KEY,
    all_presets,
    get_preset,
    resolve_base_url,
)

EXPECTED_KEYS = {
    "openai", "anthropic", "gemini", "xai", "openrouter",
    "groq", "together", "mistral", "ollama", "lmstudio", "custom",
}


class TestCatalogShape:
    def test_exact_expected_key_set(self):
        assert {p.key for p in all_presets()} == EXPECTED_KEYS

    def test_keys_are_unique(self):
        keys = [p.key for p in all_presets()]
        assert len(keys) == len(set(keys))

    def test_custom_is_last(self):
        assert all_presets()[-1].key == CUSTOM_PROVIDER_KEY

    def test_key_optional_for_local_runtimes_and_custom(self):
        # Hosted providers require a key at save; local runtimes and custom
        # OpenAI-compatible endpoints (commonly keyless vLLM/sglang) may save
        # with an empty key — the probe-gated save still refuses a keyless
        # config the endpoint actually rejects.
        keyless = {p.key for p in all_presets() if not p.key_required}
        assert keyless == {"ollama", "lmstudio", "custom"}

    def test_every_preset_has_label(self):
        assert all(p.label for p in all_presets())

    def test_only_custom_lacks_base_url(self):
        without_url = {p.key for p in all_presets() if p.base_url is None}
        assert without_url == {CUSTOM_PROVIDER_KEY}

    def test_all_preset_base_urls_parse_as_http_s(self):
        for p in all_presets():
            if p.key == CUSTOM_PROVIDER_KEY:
                continue
            parsed = urlparse(p.base_url)
            assert parsed.scheme in ("http", "https"), (
                f"{p.key}: bad scheme in {p.base_url!r}")
            assert parsed.netloc, f"{p.key}: no host in {p.base_url!r}"


class TestGetPreset:
    def test_known_key(self):
        assert get_preset("openai").base_url == "https://api.openai.com/v1"

    def test_normalizes_case_and_whitespace(self):
        assert get_preset("  OpenAI ").key == "openai"

    def test_unknown_key_returns_none(self):
        assert get_preset("does-not-exist") is None
        assert get_preset("") is None
        assert get_preset(None) is None


class TestResolveBaseUrl:
    def test_preset_ignores_submitted_url(self):
        # Server-derived: a submitted (potentially attacker-chosen) URL is
        # ignored for every non-custom preset.
        assert resolve_base_url("openai", "https://evil.example/v1") == \
            "https://api.openai.com/v1"

    def test_every_preset_resolves_to_its_catalog_url(self):
        for p in all_presets():
            if p.key == CUSTOM_PROVIDER_KEY:
                continue
            assert resolve_base_url(p.key, "https://evil.example/v1") == p.base_url

    def test_custom_honors_submitted_url(self):
        assert resolve_base_url("custom", "https://my.example/v1") == \
            "https://my.example/v1"

    def test_custom_strips_trailing_slash(self):
        assert resolve_base_url("custom", "https://my.example/v1/") == \
            "https://my.example/v1"

    def test_unknown_provider_returns_none(self):
        assert resolve_base_url("martian-ai", "https://x.example/v1") is None

    def test_custom_without_url_returns_none(self):
        assert resolve_base_url("custom", "") is None
        assert resolve_base_url("custom", "   ") is None
        assert resolve_base_url("custom") is None
