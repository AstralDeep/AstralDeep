"""Public entry point for the Astral SDK's AstralClient; only httpx is a runtime
dependency, and framework adapters under astral_sdk.integrations import their target
framework lazily so installing this never pulls in extras.
"""

from __future__ import annotations

from astral_sdk.client import AstralClient, AsyncAstralClient
from astral_sdk.errors import (
    AstralAuthError,
    AstralConflictError,
    AstralError,
    AstralHTTPError,
    AstralTimeoutError,
    RetryExhaustedError,
)
from astral_sdk.mcp_bridge import Bridge
from astral_sdk.models import Artifact, ControlResult, Event, Operation, OperationList, RetryPolicy
from astral_sdk.tools import ASTRAL_TOOLS, TOOL_NAMES

__version__ = "0.1.0"

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
