"""Authorizes global external-agent key pin changes through a fresh human socket write.
The personal-node owner or the existing administrator policy controls this application-wide trust registry.
"""

from orchestrator import auth
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.human_request_authority import _HumanSocketRequest
from persistent_agents.models import AssignmentError
from shared import node_identity


async def registration_caller(orchestrator, websocket, payload):
    pending = (_CONNECTION_OPERATION_CONTEXT.get() or {}).get("human_request")
    if (type(pending) is not _HumanSocketRequest or pending.purpose != "metadata"
            or pending.method != "WS_WRITE" or pending.boundary.orchestrator is not orchestrator
            or pending.websocket is not websocket or pending.message.get("action") != "register_external_agent"
            or pending.message.get("payload") != payload):
        raise AssignmentError("human_authentication_required", 401)
    caller = await pending.authenticate()
    caller.require_write()
    if node_identity.is_personal():
        try:
            owner = node_identity.bound_owner()
        except node_identity.NodeIdentityError:
            raise AssignmentError("node_owner_binding_unavailable", 503) from None
        if not owner or caller.owner_id != owner:
            raise AssignmentError("agent_trust_forbidden", 403)
    else:
        try:
            await auth.verify_admin(caller.claims)
        except auth.HTTPException:
            raise AssignmentError("agent_trust_forbidden", 403) from None
    return caller
