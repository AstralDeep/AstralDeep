"""Returns a copy of the current execution context with the UI operation fence cleared,
so server-spawned background tasks (agent_quick_create.py, remote_confirmation.py)
don't inherit a fence that goes stale after the handler returns.
"""

from __future__ import annotations

import contextvars
import sys


def detached_context(orch) -> contextvars.Context:
    ctx = contextvars.copy_context()
    candidates = []
    module = sys.modules.get(type(orch).__module__)
    var = getattr(module, "_CONNECTION_OPERATION_CONTEXT", None)
    if isinstance(var, contextvars.ContextVar):
        candidates.append(var)
    try:
        from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT as imported
    except Exception:  # noqa: BLE001
        imported = None
    if isinstance(imported, contextvars.ContextVar) and imported not in candidates:
        candidates.append(imported)
    for var in candidates:
        ctx.run(var.set, None)
    return ctx
