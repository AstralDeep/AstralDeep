"""REST endpoints for the LLM test-connection and model-listing probes: run a one-shot
chat-completions/models.list call against caller-supplied credentials, never persist
or log them, and audit llm_config_change(action='tested').
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from openai import OpenAI
from pydantic import BaseModel, Field, field_validator

from orchestrator.auth import get_current_user_payload, require_user_id

from .audit_events import record_llm_config_change
from .client_factory import openai_auth_kwargs
from .probe import PROBE_TIMEOUT_SECONDS, classify_probe_error as _classify_probe_error

logger = logging.getLogger("LLMConfig.API")

llm_router = APIRouter(prefix="/api/llm", tags=["LLM"])

_PROBE_RATE_PER_MINUTE = int(os.getenv("LLM_PROBE_RATE_PER_MINUTE", "20") or "20")
_probe_hits: Dict[str, Deque[float]] = defaultdict(deque)


def _check_probe_rate(user_id: str) -> None:
    now = time.monotonic()
    hits = _probe_hits[user_id]
    while hits and now - hits[0] > 60.0:
        hits.popleft()
    if len(hits) >= _PROBE_RATE_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail="Too many connection probes — wait a minute and retry.",
        )
    hits.append(now)


class TestConnectionRequest(BaseModel):
    api_key: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)

    @field_validator("base_url")
    @classmethod
    def _check_url_scheme(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")

    @field_validator("api_key", "model")
    @classmethod
    def _strip_and_check(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must be non-empty after trim")
        return v


class TestConnectionResponse(BaseModel):
    ok: bool
    model: str
    probed_at: str
    latency_ms: Optional[int] = None
    error_class: Optional[str] = None
    upstream_message: Optional[str] = None


class ListModelsRequest(BaseModel):
    api_key: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)

    @field_validator("base_url")
    @classmethod
    def _check_url_scheme(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def _strip_and_check(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must be non-empty after trim")
        return v


class ListModelsResponse(BaseModel):
    ok: bool
    models: list[str]
    probed_at: str
    latency_ms: Optional[int] = None
    error_class: Optional[str] = None
    upstream_message: Optional[str] = None


def _get_orchestrator(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return orch


@llm_router.post(
    "/test",
    response_model=TestConnectionResponse,
    summary="Probe a prospective LLM configuration with a 1-token chat-completions request",
)
async def test_connection(
    body: TestConnectionRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
    user_payload: dict = Depends(get_current_user_payload),
) -> TestConnectionResponse:
    orch = _get_orchestrator(request)
    _check_probe_rate(user_id)
    auth_principal = (
        (user_payload or {}).get("preferred_username")
        or (user_payload or {}).get("sub")
        or user_id
    )

    probed_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    error_class: Optional[str] = None
    upstream_message: Optional[str] = None
    ok = False
    try:
        client = OpenAI(
            base_url=body.base_url,
            timeout=PROBE_TIMEOUT_SECONDS,
            max_retries=0,
            **openai_auth_kwargs(body.api_key),
        )
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=body.model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        if not getattr(response, "choices", None):
            raise ValueError("response missing 'choices' — not an OpenAI-compatible chat-completions endpoint")
        first = response.choices[0]
        if not getattr(first, "message", None):
            raise ValueError("response.choices[0] missing 'message' — not an OpenAI-compatible chat-completions endpoint")
        ok = True
    except Exception as exc:
        ok = False
        error_class = _classify_probe_error(exc)
        upstream_message = str(exc)[:1024]
        logger.info(
            "Test Connection failed: error_class=%s base_url=%s model=%s",
            error_class, body.base_url, body.model,
        )
    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        await record_llm_config_change(
            orch.audit_recorder,
            actor_user_id=user_id,
            auth_principal=auth_principal,
            action="tested",
            base_url=body.base_url,
            model=body.model,
            transport="rest",
            result="success" if ok else "failure",
            error_class=error_class if not ok else None,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(f"llm_config_change(tested) audit failed (non-fatal): {exc}")

    return TestConnectionResponse(
        ok=ok,
        model=body.model,
        probed_at=probed_at,
        latency_ms=latency_ms,
        error_class=error_class,
        upstream_message=upstream_message,
    )


@llm_router.post(
    "/list-models",
    response_model=ListModelsResponse,
    summary="List model ids advertised by the user-configured LLM endpoint",
)
async def list_models(
    body: ListModelsRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
    user_payload: dict = Depends(get_current_user_payload),
) -> ListModelsResponse:
    _ = request
    _ = user_payload
    _check_probe_rate(user_id)

    probed_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    error_class: Optional[str] = None
    upstream_message: Optional[str] = None
    models: list[str] = []
    ok = False
    try:
        client = OpenAI(
            base_url=body.base_url,
            timeout=PROBE_TIMEOUT_SECONDS,
            max_retries=0,
            **openai_auth_kwargs(body.api_key),
        )
        page = await asyncio.to_thread(client.models.list)
        data = getattr(page, "data", None)
        if data is None:
            raise ValueError("response missing 'data' list — not an OpenAI-compatible /models endpoint")
        ids: set[str] = set()
        for m in data:
            mid = getattr(m, "id", None)
            if isinstance(mid, str) and mid:
                ids.add(mid)
        models = sorted(ids)
        ok = True
    except Exception as exc:
        ok = False
        models = []
        error_class = _classify_probe_error(exc)
        if error_class == "model_not_found":
            error_class = "transport_error"
        upstream_message = str(exc)[:1024]
        logger.info(
            "List Models failed: error_class=%s base_url=%s",
            error_class, body.base_url,
        )
    latency_ms = int((time.monotonic() - started) * 1000)

    return ListModelsResponse(
        ok=ok,
        models=models,
        probed_at=probed_at,
        latency_ms=latency_ms,
        error_class=error_class,
        upstream_message=upstream_message,
    )
