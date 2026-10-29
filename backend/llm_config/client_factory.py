"""Builds provider clients from resolved encrypted configuration. Compatibility remains the default; explicit native profiles preserve owner and record provenance through the same factory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Protocol, Tuple

from openai import OpenAI

from .local_endpoint import classify_endpoint
from .types import CredentialSource, LLMUnavailable, ResolvedConfig


DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS = 60.0

KEYLESS_API_KEY_SENTINEL = "not-needed"


def _strip_authorization(request) -> None:
    request.headers.pop("Authorization", None)


# Fresh client per call — the SDK closes it after use
def _keyless_http_client():
    import httpx

    return httpx.Client(event_hooks={"request": [_strip_authorization]})


def openai_auth_kwargs(api_key: str) -> dict:
    if api_key and api_key != KEYLESS_API_KEY_SENTINEL:
        return {"api_key": api_key}
    return {
        "api_key": KEYLESS_API_KEY_SENTINEL,
        "http_client": _keyless_http_client(),
    }


class LLMConfigLike(Protocol):
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True, slots=True)
class LocalInferenceFrame:
    local: bool
    endpoint_class: str
    keyless: bool
    model: str
    audit_base_url: str


def local_inference_frame(
    config: Optional[LLMConfigLike], *, allowlist: Iterable[str] = ()
) -> LocalInferenceFrame:
    if config is None:
        return LocalInferenceFrame(False, "remote", False, "", "")
    base_url = getattr(config, "base_url", "")
    base_url = base_url if type(base_url) is str else ""
    model = getattr(config, "model", "")
    model = model if type(model) is str else ""
    api_key = getattr(config, "api_key", "")
    keyless = not api_key or api_key == KEYLESS_API_KEY_SENTINEL
    verdict = classify_endpoint(base_url, allowlist=allowlist)
    return LocalInferenceFrame(
        local=verdict.local,
        endpoint_class=verdict.endpoint_class,
        keyless=keyless,
        model=model,
        audit_base_url=verdict.redacted_base_url if verdict.local else base_url,
    )


def build_llm_client(
    config: Optional[LLMConfigLike],
    source: CredentialSource,
    *,
    timeout: Optional[float] = None,
) -> Tuple[Any, CredentialSource, ResolvedConfig]:
    if source == CredentialSource.OPERATOR_DEFAULT:
        raise ValueError(
            "CredentialSource.OPERATOR_DEFAULT is retired (feature 054): "
            "the operator-default credential path no longer exists."
        )
    if config is None:
        raise LLMUnavailable(
            "No LLM configuration for this context: "
            + (
                "the user has not completed provider setup."
                if source == CredentialSource.USER
                else "no system credential has been configured by an admin."
            )
        )
    if (getattr(config, "provider", None) == "anthropic-native"
            or getattr(config, "_native_binding", None) is not None):
        from .native_provider import NATIVE_PROFILE, NATIVE_PROVIDER, native_client
        client = native_client(config, source, DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS if timeout is None else timeout)
        binding = client.binding
        return client, source, ResolvedConfig(
            base_url=binding.base_url, model=binding.model, provider=NATIVE_PROVIDER,
            profile=NATIVE_PROFILE, owner_id=binding.owner_id, config_revision=binding.revision,
        )
    kwargs = {
        "base_url": config.base_url,
        "max_retries": 0,
        "timeout": (
            DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS
            if timeout is None
            else timeout
        ),
    }
    kwargs.update(openai_auth_kwargs(config.api_key))
    client = OpenAI(**kwargs)
    return (
        client,
        source,
        ResolvedConfig(base_url=config.base_url, model=config.model),
    )


async def create_completion(client, **kwargs):
    import asyncio
    from .native_provider import NativeMessagesClient
    if isinstance(client, NativeMessagesClient):
        return await client.create_async(**kwargs)
    return await asyncio.to_thread(client.chat.completions.create, **kwargs)
