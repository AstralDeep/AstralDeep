"""Server-owned LLM provider preset catalog (feature 054-byo-llm-setup).

The single source of truth for the "popular providers" dropdown offered by
the first-run provider-setup dialog and the LLM settings surface, on every
client (Constitution XII: one server-owned definition; clients are thin
consumers of the composed surface).

Each preset carries the provider's **OpenAI-compatible** endpoint. Base URLs
for non-``custom`` presets are SERVER-DERIVED at save time (the editable
``base_url`` field is only rendered for ``custom``), so prefill/lock behavior
needs zero client logic and cannot diverge between web and native SDUI
(plan.md Design Decision 4).

``key_required=False`` presets (local runtimes) permit an empty API key; the
dialog copy notes their endpoints must be reachable *from the server*.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

CUSTOM_PROVIDER_KEY = "custom"


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """One entry in the provider dropdown.

    Attributes:
        key: Stable identifier persisted in ``user_llm_config.provider``.
        label: Display name shown in the dropdown.
        base_url: The provider's OpenAI-compatible endpoint; ``None`` only
            for the ``custom`` escape hatch (free-form field).
        key_required: Whether an API key must be supplied to save.
        key_prefix_hint: Cosmetic placeholder hint for the key field
            (e.g. ``"sk-..."``); never used for validation.
    """
    key: str
    label: str
    base_url: Optional[str]
    key_required: bool
    key_prefix_hint: str = ""


# Ordered as rendered: hosted majors, aggregators, local runtimes, custom last.
_PRESETS: Tuple[ProviderPreset, ...] = (
    ProviderPreset("openai", "OpenAI", "https://api.openai.com/v1", True, "sk-..."),
    ProviderPreset("anthropic", "Anthropic", "https://api.anthropic.com/v1", True, "sk-ant-..."),
    ProviderPreset("gemini", "Google Gemini",
                   "https://generativelanguage.googleapis.com/v1beta/openai", True, "AIza..."),
    ProviderPreset("xai", "xAI Grok", "https://api.x.ai/v1", True, "xai-..."),
    ProviderPreset("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", True, "sk-or-..."),
    ProviderPreset("groq", "Groq", "https://api.groq.com/openai/v1", True, "gsk_..."),
    ProviderPreset("together", "Together AI", "https://api.together.xyz/v1", True, ""),
    ProviderPreset("mistral", "Mistral", "https://api.mistral.ai/v1", True, ""),
    ProviderPreset("ollama", "Ollama (local)", "http://localhost:11434/v1", False, ""),
    ProviderPreset("lmstudio", "LM Studio (local)", "http://localhost:1234/v1", False, ""),
    # Custom is key-OPTIONAL: self-hosted OpenAI-compatible endpoints
    # (vLLM/sglang, gateways, the UK LLM factory) are commonly keyless, and
    # the probe-gated save still refuses a keyless config the endpoint
    # actually rejects — so honesty is enforced by the probe, not the form.
    ProviderPreset(CUSTOM_PROVIDER_KEY, "Custom OpenAI-compatible endpoint", None, False, ""),
)

_BY_KEY = {p.key: p for p in _PRESETS}


def all_presets() -> Tuple[ProviderPreset, ...]:
    """Return the ordered preset catalog (``custom`` is always last)."""
    return _PRESETS


def get_preset(key: str) -> Optional[ProviderPreset]:
    """Return the preset for ``key``, or ``None`` for an unknown key."""
    return _BY_KEY.get((key or "").strip().lower())


def resolve_base_url(provider_key: str, submitted_base_url: str = "") -> Optional[str]:
    """Derive the effective base URL for a save.

    Non-``custom`` presets ALWAYS use the catalog's endpoint (submitted
    values are ignored — the field is not rendered for them); ``custom``
    uses the caller's submitted URL. Returns ``None`` when the provider is
    unknown, or when ``custom`` was selected without a URL.
    """
    preset = get_preset(provider_key)
    if preset is None:
        return None
    if preset.key == CUSTOM_PROVIDER_KEY:
        url = (submitted_base_url or "").strip().rstrip("/")
        return url or None
    return preset.base_url
