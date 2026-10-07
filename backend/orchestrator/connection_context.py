"""Connection-scoped contextvars shared by the WebSocket hub and its chrome/surface
dispatch helpers; orchestrator.py re-exports them. Housing them here lets transport
helpers read connection state without importing the full orchestrator module.
"""

from __future__ import annotations

import contextvars
from typing import Any

_CONNECTION_OPERATION_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("connection_operation_context", default=None)
)
_WORKSPACE_MUTATION_LOCKS: contextvars.ContextVar[frozenset[str]] = (
    contextvars.ContextVar("workspace_mutation_locks", default=frozenset())
)
_ACTIVE_REQUEST_TEXT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "active_request_text", default=""
)
