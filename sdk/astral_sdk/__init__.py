"""Public entry point for the Astral SDK's AstralClient; only httpx is a runtime
dependency, and framework adapters under astral_sdk.integrations import their target
framework lazily so installing this never pulls in extras. Bridge is re-exported
through module __getattr__ so the documented `python -m astral_sdk.mcp_bridge` launch
never executes a second copy of the already-imported module.
"""

from __future__ import annotations

from typing import Any

from astral_sdk.client import AstralClient, AsyncAstralClient
from astral_sdk.errors import (
    AstralAuthError,
    AstralConflictError,
    AstralError,
    AstralHTTPError,
    AstralTimeoutError,
    RetryExhaustedError,
)
from astral_sdk.models import (
    Artifact, CompletionSubscription, CompletionSubscriptionList, ControlResult, Event,
    Operation, OperationList, RetryPolicy,
)
from astral_sdk.tools import ASTRAL_TOOLS, TOOL_NAMES

__version__ = "0.2.0"


def __getattr__(name: str) -> Any:
    if name == "Bridge":
        from astral_sdk.mcp_bridge import Bridge

        return Bridge
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})

__all__ = [
    "__version__",
    "AstralClient",
    "AsyncAstralClient",
    "Bridge",
    "Operation",
    "OperationList",
    "Event",
    "Artifact",
    "ControlResult",
    "CompletionSubscription",
    "CompletionSubscriptionList",
    "RetryPolicy",
    "AstralError",
    "AstralHTTPError",
    "AstralAuthError",
    "AstralConflictError",
    "AstralTimeoutError",
    "RetryExhaustedError",
    "ASTRAL_TOOLS",
    "TOOL_NAMES",
]
