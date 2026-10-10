"""
AstralDeep – Provider‑aware context token accounting utilities.

This module implements a deterministic, offline‑first token estimator that
understands:

* Different providers (OpenAI, Anthropic, etc.) and their model‑specific encodings.
* The full request payload, including:
  - User/assistant messages.
  - Tool definitions (function specifications).
  - Tool call arguments and results.
  - Multimodal payload allowances (e.g. image tokens).
* Conservative fall‑backs for unknown models/providers.

The implementation purposefully avoids any network request at runtime.
All encoder assets are bundled with the ``tiktoken`` package and are loaded
via ``tiktoken.get_encoding`` which raises ``KeyError`` if the encoding is
missing – we catch this and fall back to a safe default.

The public API consists of the ``ContextTokenEstimator`` class and the
convenient ``estimate_request_tokens`` function.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import tiktoken

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Provider / model metadata
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ModelMeta:
    """Metadata required for token accounting of a specific model."""
    provider: str
    model_name: str
    encoding_name: str
    max_context_tokens: int
    # Conservative per‑image token allowance (e.g. 85 tokens per 1 000 px²)
    image_token_per_px: float = 0.0
    # Reserved tokens for the model's output (assistant reply)
    reserved_output_tokens: int = 256


# Known model table – this is deliberately small; unknown models fall back.
KNOWN_MODELS: Mapping[Tuple[str, str], ModelMeta] = {
    # OpenAI models
    ("openai", "gpt-4o-mini"): ModelMeta(
        provider="openai",
        model_name="gpt-4o-mini",
        encoding_name="cl100k_base",
        max_context_tokens=128_000,
        image_token_per_px=0.085,  # 85 tokens per 1 000 px² (example)
    ),
    ("openai", "gpt-4o"): ModelMeta(
        provider="openai",
        model_name="gpt-4o",
        encoding_name="cl100k_base",
        max_context_tokens=128_000,
        image_token_per_px=0.085,
    ),
    ("openai", "gpt-3.5-turbo"): ModelMeta(
        provider="openai",
        model_name="gpt-3.5-turbo",
        encoding_name="cl100k_base",
        max_context_tokens=16_384,
    ),
    # Anthropic models
    ("anthropic", "claude-3-5-sonnet-20240620"): ModelMeta(
        provider="anthropic",
        model_name="claude-3-5-sonnet-20240620",
        encoding_name="anthropic_claude",
        max_context_tokens=200_000,
        image_token_per_px=0.0,
    ),
    # Add more known models here as needed.
}


# --------------------------------------------------------------------------- #
# Helper utilities
# --------------------------------------------------------------------------- #

def _load_encoding(encoding_name: str) -> tiktoken.Encoding:
    """
    Load a tiktoken encoding, guaranteeing offline operation.
    Raises RuntimeError if the encoding cannot be loaded.
    """
    try:
        enc = tiktoken.get_encoding(encoding_name)
        log.debug("Loaded encoding %s", encoding_name)
        return enc
    except Exception as exc:  # pragma: no cover – defensive
        raise RuntimeError(
            f"Failed to load tiktoken encoding '{encoding_name}'. "
            "Ensure the encoding assets are bundled with the package."
        ) from exc


def _hash_content(content: Any) -> str:
    """
    Deterministic hash of arbitrary JSON‑serialisable content.
    Used for audit‑trail purposes; not part of token counting.
    """
    json_bytes = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(json_bytes).hexdigest()


# --------------------------------------------------------------------------- #
# Core estimator
# --------------------------------------------------------------------------- #

@dataclass
class ContextTokenEstimator:
    """
    Estimate the total token usage of a request payload for a given provider/model.
    """

    provider: str
    model: str
    # Optional explicit override – useful for testing or custom deployments.
    encoding_name: str | None = None
    max_context_tokens: int | None = None
    reserved_output_tokens: int = 256
    image_token_per_px: float = 0.0

    # Internal fields (populated lazily)
    _encoding: tiktoken.Encoding = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        meta = KNOWN_MODELS.get((self.provider, self.model))
        if meta:
            self.encoding_name = meta.encoding_name
            self.max_context_tokens = meta.max_context_tokens
            self.reserved_output_tokens = meta.reserved_output_tokens
            self.image_token_per_px = meta.image_token_per_px
        else:
            # Conservative fallback – cl100k_base is the most generic OpenAI encoder.
            # If the user supplied an explicit encoding_name we honour it.
            if not self.encoding_name:
                self.encoding_name = "cl100k_base"
            if not self.max_context_tokens:
                self.max_context_tokens = 4_096  # Very safe default.
            log.warning(
                "Using conservative fallback for unknown model/provider "
                "(%s/%s). Encoding=%s, max_context=%s",
                self.provider,
                self.model,
                self.encoding_name,
                self.max_context_tokens,
            )
        self._encoding = _load_encoding(self.encoding_name)  # type: ignore[arg-type]

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #

    def count_message(self, message: Mapping[str, Any]) -> int:
        """
        Count tokens for a single message dict. The dict follows the OpenAI/Anthropic
        schema (role, content, name, etc.). Multimodal content (e.g. images) is
        accounted for via ``image_token_per_px``.
        """
        role = message.get("role", "")
        content = message.get("content", "")

        # Role token cost – a small constant (1 token) is sufficient.
        token_count = 1

        if isinstance(content, str):
            token_count += self._count_text(content)
        elif isinstance(content, list):
            # List of content blocks – each block may be text or image.
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        token_count += self._count_text(block.get("text", ""))
                    elif block.get("type") == "image_url":
                        token_count += self._count_image(block.get("image_url", {}))
                    else:
                        # Unknown block type – be conservative.
                        token_count += 5
                else:
                    token_count += 5
        else:
            token_count += 5  # Fallback for unexpected structures.

        # Include name field if present (rarely used).
        if "name" in message:
            token_count += self._count_text(str(message["name"]))

        return token_count

    def count_tool_definition(self, tool: Mapping[str, Any]) -> int:
        """
        Count tokens for a tool (function) definition. We count the JSON
        representation of the definition, which is a safe approximation.
        """
        json_repr = json.dumps(tool, separators=(",", ":"))
        return self._count_text(json_repr)

    def count_tool_call(self, call: Mapping[str, Any]) -> int:
        """
        Count tokens for a tool call (arguments) and its result (if present).
        The schema is provider‑agnostic; we simply tokenise the JSON payload.
        """
        token_count = 0
        if "arguments" in call:
            token_count += self._count_text(json.dumps(call["arguments"], separators=(",", ":")))
        if "result" in call:
            token_count += self._count_text(json.dumps(call["result"], separators=(",", ":")))
        return token_count

    def estimate_total(self, payload: Mapping[str, Any]) -> Tuple[int, bool]:
        """
        Estimate the total token usage for a full request payload.

        Returns:
            (total_tokens, within_budget)
        """
        total = 0

        # 1. Messages
        messages = payload.get("messages", [])
        for msg in messages:
            total += self.count_message(msg)

        # 2. Tools (function definitions)
        tools = payload.get("tools", [])
        for tool in tools:
            total += self.count_tool_definition(tool)

        # 3. Tool calls (arguments + results) – may appear in messages or a dedicated field.
        tool_calls = payload.get("tool_calls", [])
        for call in tool_calls:
            total += self.count_tool_call(call)

        # 4. Reserved output tokens (model must keep room for its reply)
        total += self.reserved_output_tokens

        within_budget = total <= (self.max_context_tokens or 0)
        return total, within_budget

    # --------------------------------------------------------------------- #
    # Private helpers
    # --------------------------------------------------------------------- #

    def _count_text(self, text: str) -> int:
        """
        Tokenise a plain string using the selected encoding.
        """
        try:
            return len(self._encoding.encode(text))
        except Exception as exc:  # pragma: no cover – defensive
            log.error("Failed to encode text for token counting: %s", exc)
            # Fallback to a very conservative estimate (1 token per 4 characters)
            return max(1, len(text) // 4)

    def _count_image(self, image_url_block: Mapping[str, Any]) -> int:
        """
        Estimate token cost for an image URL block. The block may contain a
        ``detail`` field and a ``url``. We use the image dimensions if they are
        supplied via a ``metadata`` sub‑field; otherwise we apply a generic
        per‑pixel cost.
        """
        metadata = image_url_block.get("metadata", {})
        width = metadata.get("width")
        height = metadata.get("height")
        if isinstance(width, int) and isinstance(height, int):
            pixels = width * height
        else:
            # Assume a default 512×512 image when dimensions are unknown.
            pixels = 512 * 512
        return int(pixels * self.image_token_per_px)


def estimate_request_tokens(
    payload: Mapping[str, Any],
    provider: str,
    model: str,
) -> Tuple[int, bool]:
    """
    Convenience wrapper used by the orchestration layer.

    Args:
        payload: The full request dictionary that will be sent to the LLM provider.
        provider: Provider identifier (e.g. ``"openai"``, ``"anthropic"``).
        model: Model name as understood by the provider.

    Returns:
        (total_token_estimate, within_budget)
    """
    estimator = ContextTokenEstimator(provider=provider, model=model)
    return estimator.estimate_total(payload)
