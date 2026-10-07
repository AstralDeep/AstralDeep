"""Connection-scoped contextvars shared by the orchestrator hub and its leaf dispatch
modules: the per-connection Work operation fence that chrome_events, detached_context,
agent_authoring, and human_request_authority read without importing the orchestrator
hub (whose transport dependencies must stay out of narrow import profiles).
"""

from __future__ import annotations

import contextvars
from typing import Any

CONNECTION_OPERATION_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("connection_operation_context", default=None)
)

__all__ = ["CONNECTION_OPERATION_CONTEXT"]
