from __future__ import annotations

from typing import Any

from backend.llm_config.store import get_owner_config
from backend.llm.providers.openai_compatible import OpenAICompatibleClient
from backend.llm.providers.local import LocalClient
from backend.llm.providers.anthropic_native_adapter import AnthropicNativeClient


def _require_owner_credentials(config: dict[str, Any], profile: str) -> tuple[str, str]:
    endpoint = config.get("provider_endpoint")
    api_key = config.get("provider_api_key")
    if not endpoint or not api_key:
        raise ValueError(f"Missing credentials for profile {profile}")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise ValueError("Invalid provider endpoint")
    return endpoint, api_key


def create_client(owner_id: str, model_name: str, profile_override: str | None = None) -> Any:
    config = get_owner_config(owner_id)
    if not isinstance(config, dict):
        raise ValueError("Owner config not found")

    profile = profile_override or config.get("provider_profile", "openai_compatible")
    endpoint = config.get("provider_endpoint")
    api_key = config.get("provider_api_key")
    timeout = int(config.get("provider_timeout_seconds", 30))

    if profile == "anthropic_native":
        endpoint, api_key = _require_owner_credentials(config, profile)
        if not endpoint.startswith("https://api.anthropic.com"):
            raise ValueError("Native Claude endpoint must be api.anthropic.com")
        return AnthropicNativeClient(
            owner_id=owner_id,
            endpoint=endpoint.rstrip("/") + "/v1/messages",
            api_key=api_key,
            timeout_seconds=timeout,
        )

    if profile == "local":
        return LocalClient(owner_id=owner_id, model_name=model_name)

    if profile == "openai_compatible":
        endpoint, api_key = _require_owner_credentials(config, profile)
        return OpenAICompatibleClient(
            owner_id=owner_id,
            model_name=model_name,
            endpoint=endpoint,
            api_key=api_key,
            timeout_seconds=timeout,
        )

    raise ValueError(f"Unsupported provider profile: {profile}")
