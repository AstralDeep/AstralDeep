"""Optional framework adapters — each submodule imports its target lazily.

Importing ``astral_sdk.integrations`` itself never imports OpenAI, Anthropic,
LangChain, or any other framework: this package only re-exports submodule
names. A given submodule's own functions defer the actual third-party import
to their first call, and raise :class:`ImportError` with an actionable extras
hint (``pip install astral-sdk[openai]`` etc.) when it is missing — see
``sdk/tests/test_adapters_optional.py``.
"""
from __future__ import annotations

__all__ = ["generic", "openai_agents", "anthropic", "langchain"]
