"""The sole module that imports typesafe_sdk (lazily, inside load_sdk);
api_key/base_url/model are always passed explicitly rather than read from the SDK's
own environment fallback, per budget.py's attempt/deadline clipping.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger("Orchestrator.TypeSafe.Client")

TYPESAFE_API_BASE = "https://api.typesafe.ai"

TYPESAFE_MODEL = "jev-latest"

# Sized so 3 attempts + backoff fit exactly in the turn budget
ATTEMPT_TIMEOUT_MS = 400


class TypeSafeUnavailable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SdkSurface:
    client_class: Any
    retry_policy: Any
    noul: Any
    score: Any
    choice: Any
    errors: Mapping[str, Any] = field(default_factory=dict)


_sdk_cache: Optional[SdkSurface] = None
_sdk_failed = False


def load_sdk() -> SdkSurface:
    global _sdk_cache, _sdk_failed
    if _sdk_cache is not None:
        return _sdk_cache
    if _sdk_failed:
        raise TypeSafeUnavailable("typesafe_sdk is not installed")
    try:
        import typesafe_sdk as sdk
    except ImportError as exc:
        _sdk_failed = True
        logger.info("typesafe_sdk is not installed; routing degrades to standard")
        raise TypeSafeUnavailable("typesafe_sdk is not installed") from exc

    _sdk_cache = SdkSurface(
        client_class=sdk.AsyncTypeSafeClient,
        retry_policy=sdk.RetryPolicy,
        noul=sdk.Noul,
        score=sdk.Score,
        choice=sdk.Choice,
        errors={
            name: getattr(sdk, name)
            for name in (
                "TypeSafeError",
                "TypeSafeAPIError",
                "TypeSafeAPIConnectionError",
                "TypeSafeAPITimeoutError",
                "TypeSafeAPIResponseValidationError",
                "TypeSafeAuthenticationError",
                "TypeSafePermissionDeniedError",
                "TypeSafeBadRequestError",
                "TypeSafeNotFoundError",
                "TypeSafeRateLimitError",
                "TypeSafeUnprocessableEntityError",
                "TypeSafeInternalServerError",
            )
            if hasattr(sdk, name)
        },
    )
    return _sdk_cache


def reset_sdk_cache() -> None:
    global _sdk_cache, _sdk_failed
    _sdk_cache = None
    _sdk_failed = False


def _validated_base_url(base_url: str) -> str:
    try:
        from shared.external_http import EgressBlockedError, validate_egress_url
    except ImportError:  # pragma: no cover
        return base_url
    try:
        validate_egress_url(base_url)
    except EgressBlockedError as exc:
        raise TypeSafeUnavailable(f"TypeSafe base URL is not a permitted egress target: {exc}") from exc
    return base_url


class TypeSafeAdapterClient:
    def __init__(
        self,
        *,
        base_url: str = TYPESAFE_API_BASE,
        model: str = TYPESAFE_MODEL,
        sdk: Optional[SdkSurface] = None,
        http_client: Any = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._sdk = sdk
        self._http_client = http_client
        self._http_lock = asyncio.Lock()
        self._closed = False

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    def _surface(self) -> SdkSurface:
        return self._sdk if self._sdk is not None else load_sdk()

    async def _shared_http_client(self) -> Any:
        if self._http_client is not None:
            return self._http_client
        async with self._http_lock:
            if self._http_client is None:
                if self._closed:
                    raise TypeSafeUnavailable("the TypeSafe adapter client is closed")
                try:
                    import httpx2
                except ImportError as exc:
                    raise TypeSafeUnavailable("httpx2 is not installed") from exc
                self._http_client = httpx2.AsyncClient()
        return self._http_client

    async def system_one(
        self,
        *,
        api_key: str,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
        timeout: float,
    ) -> Any:
        surface = self._surface()
        base_url = _validated_base_url(self._base_url)
        http_client = await self._shared_http_client()
        client = surface.client_class(
            api_key=api_key,
            base_url=base_url,
            model=self._model,
            retry=surface.retry_policy(max_retries=0),
            timeout=timeout,
            http_client=http_client,
        )
        return await client.system_one(state=dict(state), questions=dict(questions))

    async def aclose(self) -> None:
        self._closed = True
        client, self._http_client = self._http_client, None
        if client is None:
            return
        closer = getattr(client, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:  # pragma: no cover
            logger.debug("closing the TypeSafe HTTP client failed", exc_info=True)


_adapter_client: Optional[TypeSafeAdapterClient] = None


def _maybe_fault_injecting(client: TypeSafeAdapterClient) -> Any:
    import os

    if not (os.getenv("ASTRAL_TEST_TYPESAFE_FAULT") or "").strip():
        return client
    try:
        from tests.fakes.typesafe_fake import FaultInjectingClient, configured_fault
    except Exception:  # pragma: no cover
        logger.warning(
            "ASTRAL_TEST_TYPESAFE_FAULT is set but the fault injector is not "
            "importable; continuing with the real client"
        )
        return client
    if configured_fault() is None:
        return client
    logger.warning(
        "TypeSafe fault injection is ACTIVE (%s). This is a development-only "
        "switch; routing calls will fail deliberately.",
        os.environ.get("ASTRAL_TEST_TYPESAFE_FAULT"),
    )
    return FaultInjectingClient(client)


def adapter_client() -> TypeSafeAdapterClient:
    global _adapter_client
    if _adapter_client is None:
        _adapter_client = _maybe_fault_injecting(TypeSafeAdapterClient())
    return _adapter_client


def set_adapter_client(client: Optional[TypeSafeAdapterClient]) -> None:
    global _adapter_client
    _adapter_client = client


async def shutdown_adapter_client() -> None:
    global _adapter_client
    client, _adapter_client = _adapter_client, None
    if client is not None:
        await client.aclose()


__all__ = (
    "ATTEMPT_TIMEOUT_MS",
    "TYPESAFE_API_BASE",
    "TYPESAFE_MODEL",
    "SdkSurface",
    "TypeSafeAdapterClient",
    "TypeSafeUnavailable",
    "adapter_client",
    "load_sdk",
    "reset_sdk_cache",
    "set_adapter_client",
    "shutdown_adapter_client",
)
