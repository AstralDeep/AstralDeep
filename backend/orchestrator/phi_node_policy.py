"""Refuses to start a draft agent on a node that is a member of the owner's PHI group, because draft agents run unconfined and would sit beside PHI bodies the node can open.
It reads membership from the Kernel through node_mesh and is called by agent_lifecycle.start_draft_agent, and api.py reports the refusal with its code and reason."""

from __future__ import annotations

from orchestrator import node_mesh

CODE = "exec.agent_server_unsupported"
REASON = "phi_node"
MESSAGE = "This node is in your PHI group, so agents you create cannot run on it. Exclude the node from the PHI group to run them here."


class AgentServerUnsupported(ValueError):
    code = CODE
    reason = REASON

    def __init__(self) -> None:
        super().__init__(MESSAGE)

    def detail(self) -> dict:
        return {"code": CODE, "reason": REASON, "message": MESSAGE}


async def refuse_on_phi_node() -> None:
    mesh = node_mesh.current()
    if mesh is None or not mesh.started:
        return
    if not (await mesh.status())["enrolled"]:
        return
    try:
        member = await mesh.phi_member()
    except mesh.binding.KernelError:
        member = True
    if member:
        raise AgentServerUnsupported()
