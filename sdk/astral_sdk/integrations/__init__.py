"""Re-exports the optional framework adapter submodules; importing this package alone
never imports OpenAI, Anthropic, or LangChain, since each adapter defers its
third-party import to first call.
"""

from __future__ import annotations

__all__ = ["generic", "openai_agents", "anthropic", "langchain"]
