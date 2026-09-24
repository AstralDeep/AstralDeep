"""Server-owned preset catalog for the LLM provider dropdown: base URLs for non-custom
presets are always server-derived at save time, so resolve_base_url ignores a
submitted URL for anything but the 'custom' escape hatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

CUSTOM_PROVIDER_KEY = "custom"


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    key: str
    label: str
    base_url: Optional[str]
    key_required: bool
    key_prefix_hint: str = ""


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
    ProviderPreset(CUSTOM_PROVIDER_KEY, "Custom OpenAI-compatible endpoint", None, False, ""),
)

_BY_KEY = {p.key: p for p in _PRESETS}


def all_presets() -> Tuple[ProviderPreset, ...]:
    return _PRESETS


def get_preset(key: str) -> Optional[ProviderPreset]:
    return _BY_KEY.get((key or "").strip().lower())


def resolve_base_url(provider_key: str, submitted_base_url: str = "") -> Optional[str]:
    preset = get_preset(provider_key)
    if preset is None:
        return None
    if preset.key == CUSTOM_PROVIDER_KEY:
        url = (submitted_base_url or "").strip().rstrip("/")
        return url or None
    return preset.base_url
