"""Astral SDK — Python client for Deep's owner-issued framework credentials.

::

    from astral_sdk import AstralClient

    client = AstralClient("https://your-astral-instance", token)
    op = client.submit_operation(idempotency_key="...", name="Draft a note",
                                 instructions="Write one short paragraph.")
    op = client.wait_for_terminal(op.id)

Only ``httpx`` is a runtime dependency. Framework-specific adapters live
under :mod:`astral_sdk.integrations` and import their target framework only
inside a function body, so installing this package never pulls in OpenAI,
Anthropic, LangChain, or any other optional extra.
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
