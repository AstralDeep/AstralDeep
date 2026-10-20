"""Registers faithful current UI connection contexts for terminal boundary tests.
The production orchestrator owns the context and registration state.
"""

from orchestrator.orchestrator import Orchestrator


def registered_connection(orch, socket):
    context = Orchestrator._new_connection_context(orch, socket)
    context.registered = True
    return context
