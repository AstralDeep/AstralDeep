"""Returns a copy of the current execution context with the UI operation fence cleared,
so server-spawned background tasks (agent_quick_create.py, remote_confirmation.py)
don't inherit a fence that goes stale after the handler returns.
"""

from __future__ import annotations

import contextvars
import sys


def detached_context(orch) -> contextvars.Context:
    ctx = contextvars.copy_context()
    from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT

    candidates = [_CONNECTION_OPERATION_CONTEXT]
    module = sys.modules.get(type(orch).__module__)
    var = getattr(module, "_CONNECTION_OPERATION_CONTEXT", None)
    if isinstance(var, contextvars.ContextVar) and var not in candidates:
        candidates.append(var)
    for var in candidates:
        ctx.run(var.set, None)
    return ctx
