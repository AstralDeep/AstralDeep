"""The only module in AstralDeep that imports ``typesafe_sdk`` (feature 089).

Everything about talking to TypeSafe is confined here so the rest of the
orchestrator can be written against plain dataclasses, and so the adapter can
be swapped for a deterministic fake at exactly one boundary in tests.

Three rules shape this module:

**The import is lazy.** ``typesafe_sdk`` is imported inside
:func:`load_sdk`, never at module scope. A deployment without the package
installed must degrade to standard routing, not fail to boot, so an
``ImportError`` here is an ordinary "TypeSafe is unavailable" outcome.

**Nothing is read from the environment.** ``api_key``, ``base_url`` and
``model`` are always passed explicitly. The SDK falls back to
``TYPESAFE_API_KEY`` / ``TYPESAFE_BASE_URL`` / ``TYPESAFE_DEFAULT_MODEL`` when
they are omitted, and feature 089 forbids exactly that (FR-005): a production
process refuses to boot with those names set, and the tests monkeypatch them to
sentinels to prove no code path consults them.

**Retrying belongs to the caller.** The client is constructed with
``RetryPolicy(max_retries=0)``. :mod:`.budget` owns the attempt schedule,
because the 1.5-second turn deadline is a property of the turn, not of one
request, and an SDK-internal retry would spend that budget invisibly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger("Orchestrator.TypeSafe.Client")

#: The TypeSafe API base. This is a **code constant**, not configuration.
#: Making it configurable would reintroduce the environment surface FR-005
#: exists to remove, and there is no deployment that legitimately needs a
#: different endpoint for a per-user third-party credential.
TYPESAFE_API_BASE = "https://api.typesafe.ai"

#: The System One model used for routing. Also a code constant, so a routing
#: decision is reproducible from a commit rather than from a host's env.
TYPESAFE_MODEL = "jev-latest"

#: Per-attempt wall clock, in milliseconds. :mod:`.budget` clips this to the
#: remaining turn budget.
#:
#: Measured 2026-09-17 (T015) against the real service: p50 186-240 ms and p95
#: 205-365 ms, essentially flat from 4 to 260 options, with a p99 of 611 ms.
#: 400 ms is the largest value that still fits three full attempts plus both
#: backoffs inside the 1.5 s turn budget (400 + 100 + 400 + 200 + 400 = 1500),
#: and it clears the observed p95 with margin. A p99 outlier loses its first
#: attempt and is retried, which is the trade this budget is for: a rare extra
#: round trip costs less than a turn that cannot retry at all.
ATTEMPT_TIMEOUT_MS = 400


class TypeSafeUnavailable(Exception):
    """TypeSafe cannot be reached at all: no SDK, or a refused base URL.

    Distinct from an API error. An API error means the request was made and
    failed; this means it was never made, so there is nothing to retry.
    """


@dataclass(frozen=True, slots=True)
class SdkSurface:
    """The pieces of ``typesafe_sdk`` the adapter uses, resolved once.

    Holding them in one object keeps the lazy import in a single place and
    makes the fake's job obvious: provide these attributes.
    """

    client_class: Any
    retry_policy: Any
    noul: Any
    score: Any
    choice: Any
    errors: Mapping[str, Any] = field(default_factory=dict)


_sdk_cache: Optional[SdkSurface] = None
_sdk_failed = False


def load_sdk() -> SdkSurface:
    """Import ``typesafe_sdk`` and return the surface the adapter uses.

    Raises :class:`TypeSafeUnavailable` when the package is absent. The failure
    is cached: a deployment without the package would otherwise pay an import
    attempt on every turn.
    """
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
    """Forget the cached SDK surface. Tests use this; production does not."""
    global _sdk_cache, _sdk_failed
    _sdk_cache = None
    _sdk_failed = False


def _validated_base_url(base_url: str) -> str:
    """Return ``base_url`` after the shared egress check accepts it.

    Routing is outbound traffic carrying the user's request text, so it goes
    through the same egress validation every other external call does. A base
    URL that resolves to a private address is refused rather than dialled.
    """
    try:
        from shared.external_http import EgressBlockedError, validate_egress_url
    except ImportError:  # pragma: no cover - the helper is always present
        return base_url
    try:
        validate_egress_url(base_url)
    except EgressBlockedError as exc:
        raise TypeSafeUnavailable(f"TypeSafe base URL is not a permitted egress target: {exc}") from exc
    return base_url


class TypeSafeAdapterClient:
    """One shared HTTP client, one ``system_one`` call per attempt.

    The underlying ``httpx2.AsyncClient`` is owned by this adapter and shared
    across users and turns: connection reuse is most of the difference between
    a routing call that fits in the budget and one that does not. It is closed
    on :meth:`aclose`, which the orchestrator calls at shutdown.

    Per-user clients are *not* cached, because a cache keyed by user would hold
    key material in memory for the process lifetime. The SDK client object is
    cheap; the connection pool underneath it is what is worth sharing.
    """

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
        """Create the shared connection pool on first use."""
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
        """Issue exactly one System One request.

        Every constructor argument is explicit. ``retry`` is zero attempts:
        :mod:`.budget` decides whether there is time for another try.
        """
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
        """Close the shared connection pool. Safe to call more than once."""
        self._closed = True
        client, self._http_client = self._http_client, None
        if client is None:
            return
        closer = getattr(client, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:  # pragma: no cover - shutdown is best-effort
            logger.debug("closing the TypeSafe HTTP client failed", exc_info=True)


_adapter_client: Optional[TypeSafeAdapterClient] = None


def adapter_client() -> TypeSafeAdapterClient:
    """Return the process-wide adapter client, creating it on first use."""
    global _adapter_client
    if _adapter_client is None:
        _adapter_client = TypeSafeAdapterClient()
    return _adapter_client


def set_adapter_client(client: Optional[TypeSafeAdapterClient]) -> None:
    """Install a client (or clear it). Tests and shutdown use this."""
    global _adapter_client
    _adapter_client = client


async def shutdown_adapter_client() -> None:
    """Close and drop the process-wide client."""
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
