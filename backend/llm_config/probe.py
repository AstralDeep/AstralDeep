"""Runs bounded prospective and post-save provider checks for api.py and ws_handlers.py.
Classifies a minimal chat completion without storing or logging credentials.
"""

from __future__ import annotations

import asyncio
from typing import Optional, Tuple

from openai import OpenAI

PROBE_TIMEOUT_SECONDS: float = 8.0


def classify_probe_error(exc: BaseException) -> str:
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


async def probe_native_saved(config, *, timeout=PROBE_TIMEOUT_SECONDS, source=None):
    from .client_factory import build_llm_client, create_completion
    from .types import CredentialSource
    try:
        client, _, resolved = build_llm_client(config, CredentialSource.USER if source is None else source, timeout=timeout)
        response = await create_completion(client, model=resolved.model,
            messages=[{"role": "user", "content": "ping"}], max_tokens=1)
        if not response.choices or response.choices[0].message is None:
            raise ValueError
        return True, None, None
    except TimeoutError:
        return False, "transport_error", "Native provider probe timed out"
    except Exception as error:
        if getattr(error, "code", None) == "native_response_truncated":
            return True, None, None
        status = getattr(error, "status_code", None)
        category = ("auth_failed" if status in (401, 403) else "model_not_found" if status == 404
                    else "provider_unavailable" if status == 429 or status is not None and status >= 500
                    else "contract_violation")
        return False, category, "Native provider configuration or response was unavailable"
