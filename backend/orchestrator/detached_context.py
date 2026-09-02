"""A context for server-initiated background work spawned from a UI operation.

A task created inside a ``ui_event`` handler inherits that operation's
execution fence through ``_CONNECTION_OPERATION_CONTEXT``; the fence goes stale
the moment the handler returns, so anything fenced the task does later
(conversation staging, draft CAS transitions) aborts as "ownership changed".
Feature 076 hit this with the post-approval continuation turn, feature 077
with the quick-create pipeline. ``detached_context`` returns a copy of the
current context with that operation cleared, so the task takes the detached
("internal caller") paths instead.

The variable is resolved from the module the RUNNING orchestrator instance's
class came from: in production that module is ``__main__`` (``python
orchestrator/orchestrator.py``), and importing ``orchestrator.orchestrator``
there yields a second module instance with a different ContextVar — clearing
only that one leaves the real fence in place (seen live, 2026-09-02).
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
    except Exception:  # noqa: BLE001 — a test double without the contextvar
        imported = None
    if isinstance(imported, contextvars.ContextVar) and imported not in candidates:
        candidates.append(imported)
    for var in candidates:
        ctx.run(var.set, None)
    return ctx
