"""Names the SSH mutators that need a durable dispatch reservation and refuses them, because no session authority exists to reserve one.
orchestrator.py asks it before every tool call, and remote_confirmation.py, remote_effect_ingress.py and the remote-control agent use the same tool list.
"""

from __future__ import annotations

from orchestrator.work_reconciliation import ReconciliationRefused

_TOOLS = frozenset({"make_directory", "remove_path"})


def qualified_tool(agent_id, tool_name):
    return agent_id == "remote-compute-1" and tool_name in _TOOLS


async def select_effect(orch, websocket, agent_id, tool_name, args, owner_id):
    if not qualified_tool(agent_id, tool_name):
        return None
    raise ReconciliationRefused("effect_session_required")


def run_qualified(transport, target, argv, *, tool, owner_id, timeout):
    from orchestrator.remote_transport import RemoteResult, Verdict
    return RemoteResult(verdict=Verdict.UNATTENDED_REFUSED, machine=target.label,
                        next_action="effect_current_authority_unavailable", retryable=False)
