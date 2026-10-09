from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass
class ToolCall:
    """
    Representation of a tool call extracted from a LLM response.

    The Gemini provider can attach additional opaque metadata under
    ``extra_content`` – for example ``extra_content.google.thought_signature``.
    This field is optional and is preserved verbatim so that downstream
    validation (e.g. Google’s Gemini “thought signature” requirement) can
    succeed.
    """
    id: str
    name: str
    arguments: Dict[str, Any]

    # Provider‑specific opaque metadata.  The structure is not interpreted by
    # AstralDeep; it is merely carried through the streaming accumulator,
    # reconstruction, and the outbound response payload.
    extra_content: Optional[Dict[str, Any]] = field(default=None)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the ToolCall for JSON transport."""
        base = {
            "id": self.id,
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }
        if self.extra_content is not None:
            # Preserve the exact provider‑specific nesting.
            base["extra_content"] = self.extra_content
        return base

    @classmethod
    def from_provider_dict(cls, data: Dict[str, Any]) -> "ToolCall":
        """
        Build a ToolCall from the raw dict supplied by a provider response.
        The provider may include an ``extra_content`` key with arbitrary
        provider‑specific data – it is stored unchanged.
        """
        function = data.get("function", {})
        return cls(
            id=data.get("id", ""),
            name=function.get("name", ""),
            arguments=function.get("arguments", {}),
            extra_content=data.get("extra_content"),
        )
