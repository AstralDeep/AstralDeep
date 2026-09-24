#!/usr/bin/env python3
"""LLM-Factory tool slice for the ML Services agent: list/chat/embed/transcribe against
an OpenAI-compatible Router deployment via _wrapper.py's ExternalServiceClient;
merged into the union registry by mcp_tools.py.
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
    return _wrapper.ExternalServiceClient(credentials, BUNDLE)


def _build_client(kwargs: Dict[str, Any]) -> _wrapper.ExternalServiceClient:
    return _wrapper.build_client(kwargs, BUNDLE)


def _user_facing_error(exc: Exception, service: str = "LLM-Factory") -> str:
    return _wrapper.user_facing_error(exc, service)


def _credentials_check(**kwargs) -> Dict[str, Any]:
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
