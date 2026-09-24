"""Package for bring-your-own-LLM configuration: UserLLMConfigStore holds
per-user/system encrypted credentials, providers.py is the preset catalog, and
client_factory.build_llm_client turns a resolved record into an OpenAI client.
"""

from .types import CredentialSource, LLMUnavailable, ResolvedConfig
from .client_factory import build_llm_client
from .user_store import PersistedLLMConfig, UserLLMConfigStore
from .providers import ProviderPreset, all_presets, get_preset, resolve_base_url

__all__ = [
    "CredentialSource",
    "LLMUnavailable",
    "ResolvedConfig",
    "build_llm_client",
    "PersistedLLMConfig",
    "UserLLMConfigStore",
    "ProviderPreset",
    "all_presets",
    "get_preset",
    "resolve_base_url",
]
