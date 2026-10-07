"""Shared connection-operation fence for the orchestrator hub and its sub-surfaces
(chrome_events, human_request_authority, agent_authoring, detached_context): one
ContextVar object, re-exported by orchestrator.py, importable without the hub so
narrow UI profiles do not pull the full server runtime."""

from __future__ import annotations

import contextvars
from typing import Any

_CONNECTION_OPERATION_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("connection_operation_context", default=None)
)
