#!/usr/bin/env python3
"""LLM-Factory tools for the ML Services agent (ported from ``agents/llm_factory``).

Wraps an LLM-Factory Router deployment — a pure OpenAI-compatible reverse
proxy. Curated tool surface (all names unchanged by the 029 consolidation):

- ``list_models``        — GET /v1/models
- ``chat_with_model``    — POST /v1/chat/completions (synchronous)
- ``create_embedding``   — POST /v1/embeddings
- ``transcribe_audio``   — POST /v1/audio/transcriptions (multipart)
- ``_credentials_check`` — internal probe (GET /v1/models; dispatched
                           per-bundle by the union registry)

All tools are synchronous; Router-2 exposes no long-running operations
suitable for chat use, so ``LONG_RUNNING_TOOLS`` is empty.
"""
import logging
import os
import sys
from typing import Any, Dict, List, Set, Union

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.attachment_resolver import open_attachment_blob_reader
from shared.external_http import ExternalHttpError
from astralprims import Alert, Card, Text

from agents.ml_services import _wrapper
from agents.ml_services._wrapper import (
    LLM_FACTORY_BUNDLE as BUNDLE,
    ui as _ui,
)

logger = logging.getLogger("MlServicesLlmFactoryTools")

LONG_RUNNING_TOOLS: Set[str] = set()


def make_client(credentials: Dict[str, str]) -> _wrapper.ExternalServiceClient:
    """Build an HTTP client scoped to the LLM-Factory credential bundle.

    Args:
        credentials: Decrypted credential map containing ``LLM_FACTORY_URL``
            and ``LLM_FACTORY_API_KEY``.

    Returns:
        An (unvalidated) :class:`~agents.ml_services._wrapper.ExternalServiceClient`
        (a trailing ``/v1`` on the base URL is stripped automatically).
    """
    return _wrapper.ExternalServiceClient(credentials, BUNDLE)


def _build_client(kwargs: Dict[str, Any]) -> _wrapper.ExternalServiceClient:
    """Resolve and validate the LLM-Factory client from tool kwargs.

    Args:
        kwargs: The tool call's ``**kwargs`` carrying ``_credentials`` (and
            ``_credentials_stale`` when decryption silently failed).

    Returns:
        A validated client.

    Raises:
        ValueError: When credentials are absent, stale, or incomplete.
    """
    return _wrapper.build_client(kwargs, BUNDLE)


def _user_facing_error(exc: Exception, service: str = "LLM-Factory") -> str:
    """Map an HTTP-egress exception to the user-facing chat-rendered string.

    Args:
        exc: The exception raised by the upstream call.
        service: Service label for the message; defaults to ``"LLM-Factory"``.

    Returns:
        A one-line actionable error message.
    """
    return _wrapper.user_facing_error(exc, service)


def _credentials_check(**kwargs) -> Dict[str, Any]:
    """Probe ``GET /v1/models`` — Router-2 always serves this when auth is valid.

    Args:
        **kwargs: Tool kwargs carrying ``_credentials``.

    Returns:
        A ``{"credential_test": ...}`` verdict dict.
    """
    try:
        client = _build_client(kwargs)
    except ValueError as e:
        return {"credential_test": "unexpected", "detail": str(e)}
    try:
        resp = client.get("/v1/models")
        if resp.status_code == 200:
            return {"credential_test": "ok"}
        return {"credential_test": "unexpected", "detail": f"HTTP {resp.status_code}"}
    except ExternalHttpError as e:
        return _wrapper.verdict_for_exception(e)


def list_models(**kwargs):
    """List models served by the user's LLM-Factory Router deployment.

    Surfaces the richer Router-2 fields (``id``, ``owned_by``,
    ``max_model_len``) when the upstream provides them.

    Args:
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a model-list Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        resp = client.get("/v1/models")
        payload = resp.json() if resp.content else {}
        models = payload.get("data", []) if isinstance(payload, dict) else []
        lines = []
        for m in models:
            if not isinstance(m, dict):
                continue
            pieces = [m.get("id") or m.get("name") or "?"]
            if m.get("owned_by"):
                pieces.append(f"({m['owned_by']})")
            if m.get("max_model_len"):
                pieces.append(f"context={m['max_model_len']}")
            lines.append("• " + " ".join(pieces))
        body = "\n".join(lines) or "(no models registered)"
        return _ui(
            [Card(title="LLM-Factory models", content=[Text(content=body)])],
            data={"models": models},
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def chat_with_model(model_id: str, messages: List[Dict[str, str]],
                    options: Dict[str, Any] = None, **kwargs):
    """Send a synchronous chat completion to the user's chosen Router-2 model.

    Args:
        model_id: Identifier of a model served by the Router.
        messages: OpenAI-style ``[{role, content}, ...]`` message list.
        options: Extra OpenAI-compatible parameters (temperature, max_tokens, …).
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a reply Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        body = {
            "model": model_id,
            "messages": messages,
        }
        if options:
            body.update(options)
        resp = client.post("/v1/chat/completions", json_body=body)
        payload = resp.json() if resp.content else {}
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        content = ""
        if choices and isinstance(choices[0], dict):
            content = (choices[0].get("message", {}) or {}).get("content", "") or ""
        usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        return _ui(
            [Card(title=f"Reply from {model_id}", content=[Text(content=content or "(empty reply)")])],
            data={"content": content, "model_id": model_id, "usage": usage},
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def create_embedding(model_id: str, input: Union[str, List[str]], **kwargs):
    """Compute embeddings for ``input`` using the named model.

    Mirrors the OpenAI ``/v1/embeddings`` shape: pass a single string or a
    list of strings, get back a list of vectors plus the upstream usage.

    Args:
        model_id: Identifier of an embedding-capable model.
        input: A single string or list of strings to embed.
        **kwargs: Tool kwargs (``_credentials``).

    Returns:
        An MCP UI response dict with a summary Card and ``_data`` (vectors).
    """
    try:
        client = _build_client(kwargs)
        if input is None or (isinstance(input, str) and not input.strip()):
            raise ValueError("'input' is required and must not be empty.")
        body = {"model": model_id, "input": input}
        resp = client.post("/v1/embeddings", json_body=body)
        payload = resp.json() if resp.content else {}
        data = payload.get("data", []) if isinstance(payload, dict) else []
        embeddings = [d.get("embedding") for d in data if isinstance(d, dict)]
        dim = len(embeddings[0]) if embeddings and isinstance(embeddings[0], list) else 0
        usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
        summary = (
            f"Model: {model_id}\n"
            f"Vectors: {len(embeddings)}\n"
            f"Dimension: {dim}"
        )
        return _ui(
            [Card(title="Embeddings created", content=[Text(content=summary)])],
            data={
                "embeddings": embeddings,
                "model_id": model_id,
                "usage": usage,
                "dimension": dim,
                "count": len(embeddings),
            },
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


def transcribe_audio(model_id: str, file_handle: str,
                     language: str = None, **kwargs):
    """Transcribe an uploaded audio file using the named transcription model.

    Resolves ``file_handle`` through a bounded Plane reader (per-user ownership
    enforced) and submits the bytes as ``multipart/form-data`` to
    ``/v1/audio/transcriptions``.

    Args:
        model_id: Identifier of a transcription-capable model (e.g. whisper-1).
        file_handle: AstralDeep attachment_id of the audio file.
        language: Optional ISO-639-1 language hint (e.g. ``'en'``).
        **kwargs: Tool kwargs (``_credentials``, ``user_id``).

    Returns:
        An MCP UI response dict with a transcription Card and ``_data``.
    """
    try:
        client = _build_client(kwargs)
        user_id = kwargs.get("user_id")
        if not user_id:
            raise ValueError("user_id is required to resolve attachments")
        form = {"model": model_id}
        if language:
            form["language"] = language
        with open_attachment_blob_reader(file_handle, user_id) as (attachment, reader):
            filename = attachment.filename
            payload_bytes = b"".join(reader.iter_chunks())
        files = {"file": (filename, payload_bytes, "application/octet-stream")}
        resp = client.post(
            "/v1/audio/transcriptions",
            files=files,
            data=form,
        )
        payload = resp.json() if resp.content else {}
        text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
        return _ui(
            [Card(title=f"Transcription from {model_id}",
                  content=[Text(content=text or "(empty transcription)")])],
            data={"text": text, "model_id": model_id, "filename": filename},
        )
    except (ExternalHttpError, ValueError) as e:
        return _ui([Alert(message=_user_facing_error(e), variant="error")], retryable=False)


# ---------------------------------------------------------------------------
# Tool registry (LLM-Factory slice — merged into the union by mcp_tools)
# ---------------------------------------------------------------------------


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "list_models": {
        "function": list_models,
        "description": "List models served by your LLM-Factory Router deployment.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "scope": "tools:read",
    },
    "chat_with_model": {
        "function": chat_with_model,
        "description": "Send a synchronous chat completion to a chosen LLM-Factory Router model (OpenAI-compatible).",
        "input_schema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "Identifier of a model served by the Router."},
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
                "options": {"type": "object", "description": "OpenAI-compatible parameters: temperature, max_tokens, etc."},
            },
            "required": ["model_id", "messages"],
        },
        "scope": "tools:write",
    },
    "create_embedding": {
        "function": create_embedding,
        "description": "Compute embedding vectors for a string or list of strings using a chosen model.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "Identifier of an embedding-capable model."},
                "input": {
                    "description": "Either a single string or a list of strings to embed.",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
            },
            "required": ["model_id", "input"],
        },
        "scope": "tools:write",
    },
    "transcribe_audio": {
        "function": transcribe_audio,
        "description": "Transcribe an uploaded audio file (multipart) using a chosen transcription model.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "Identifier of a transcription-capable model (e.g. whisper-1)."},
                "file_handle": {"type": "string", "description": "AstralDeep attachment_id of the audio file."},
                "language": {"type": "string", "description": "Optional ISO-639-1 language hint (e.g. 'en')."},
            },
            "required": ["model_id", "file_handle"],
        },
        "scope": "tools:write",
    },
}
