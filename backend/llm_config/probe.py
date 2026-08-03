"""Shared connection-probe helper (features 006 + 054).

One implementation of the real ``chat.completions.create(max_tokens=1)``
probe, used by both the REST endpoint (``POST /api/llm/test``) and — since
feature 054 — the server-side save path (``llm_config_set`` /
``chrome_llm_save`` / the admin system-credential save), which MUST NOT
persist a configuration that has not just passed a probe (spec FR-008).

Credentials are used transiently for a one-shot client and discarded; they
are never stored or logged here.
"""
from __future__ import annotations

import asyncio
from typing import Optional, Tuple

from openai import OpenAI

# Feature 060 (FR-055/FR-056): the provider probe must leave enough of the
# server-owned ten-second credential-save attempt for persistence, gate
# unlock, and durable terminalization.  The SDK timeout alone is not a hard
# async bound (DNS/TLS and a stuck worker thread can outlive it), so the call
# below also has an event-loop-owned deadline.
PROBE_TIMEOUT_SECONDS: float = 8.0


def classify_probe_error(exc: BaseException) -> str:
    """Map an OpenAI-SDK exception to a Test-Connection ``error_class``
    (taxonomy from specs/006-user-llm-config/contracts/rest-llm-test.md).
    """
    s = str(exc).lower()
    if (
        "401" in s
        or "403" in s
        or "auth" in s
        or "api key" in s
        or "unauthor" in s
        or "forbidden" in s
    ):
        return "auth_failed"
    if "404" in s or ("model" in s and ("not" in s or "exist" in s)):
        return "model_not_found"
    if "400" in s or "bad request" in s or "invalid_request" in s:
        return "contract_violation"
    if any(k in s for k in ("429", "500", "502", "503", "504", "rate limit", "unavailable")):
        return "provider_unavailable"
    if any(k in s for k in ("connection", "timeout", "network", "dns", "resolve")):
        return "transport_error"
    if "choices" in s or "schema" in s or "json" in s:
        return "contract_violation"
    return "other"


async def probe_chat_completion(
    *, api_key: str, base_url: str, model: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Run the minimal chat-completions probe.

    Returns ``(ok, error_class, upstream_message)`` — ``error_class`` and
    ``upstream_message`` are ``None`` on success. Never raises.
    """
    def _run():
        from .client_factory import openai_auth_kwargs

        client = OpenAI(
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
            **openai_auth_kwargs(api_key),
        )
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        if not getattr(response, "choices", None):
            raise ValueError(
                "response missing 'choices' — not an OpenAI-compatible "
                "chat-completions endpoint")
        if not getattr(response.choices[0], "message", None):
            raise ValueError(
                "response.choices[0] missing 'message' — not an "
                "OpenAI-compatible chat-completions endpoint")

    try:
        await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
        return True, None, None
    except TimeoutError:
        return False, "transport_error", "Provider probe timed out"
    except Exception as exc:
        return False, classify_probe_error(exc), str(exc)[:1024]
