"""Builds a signed synthetic human-socket turn over a real AstralPlane graph for focused
tests, exercising orchestrator/human_request_authority.py's real signature and
composition checks.
"""

from __future__ import annotations

from contextlib import contextmanager
from tempfile import TemporaryDirectory
import uuid

from jose import jwt

from verification.drivers.fixture_identity import FixtureIdentity


@contextmanager
def registered_human_turn(orch, websocket, message, chat_id, *, user_id):
    from orchestrator.human_request_authority import capture_human_socket_request

    identity = getattr(orch, "_fixture_identity", None)
    owns_identity = identity is None
    if owns_identity:
        identity = FixtureIdentity("__verif__chat_fixture")
    from orchestrator.knowledge_synthesis import KnowledgeIndex

    knowledge = TemporaryDirectory(prefix="astral-test-guidance-")
    old_knowledge = getattr(orch, "knowledge_index", None)
    orch.knowledge_index = KnowledgeIndex(knowledge.name)
    previous = orch.ui_sessions.get(websocket)
    old_context = orch._connection_contexts.get(id(websocket))
    pending = None
    try:
        claims, _ = identity.claims_and_token({"sub": identity.run_id + "_owner"})
        claims.update(sub=user_id, preferred_username=user_id,
                      realm_access={"roles": ["user"]})
        token = jwt.encode(claims, identity.private_key, algorithm="RS256",
                           headers={"kid": identity.kid})
        websocket.scope = {"type": "websocket", "headers": [], "query_string": b""}
        websocket.closed = False
        websocket._closed = False
        websocket._guidance_machine_authority = None
        orch.ui_sessions[websocket] = claims | {"_raw_token": token}
        context = orch._new_connection_context(websocket)
        context.registered = True
        context.connection_generation = uuid.uuid4()
        frame = {"type": "ui_event", "action": "chat_message", "message": message,
                 "chat_id": chat_id, "request_generation": str(uuid.uuid4()),
                 "submission_id": str(uuid.uuid4()),
                 "connection_generation": str(context.connection_generation)}
        pending = capture_human_socket_request(
            orch.human_request_boundary, websocket=websocket, context=context,
            message=frame, purpose="skill_lookup",
        )
        yield pending, frame
    finally:
        if pending is not None:
            pending.close()
        if previous is None:
            orch.ui_sessions.pop(websocket, None)
        else:
            orch.ui_sessions[websocket] = previous
        if old_context is None:
            orch._connection_contexts.pop(id(websocket), None)
        else:
            orch._connection_contexts[id(websocket)] = old_context
        if owns_identity:
            identity.close()
        if old_knowledge is None:
            del orch.knowledge_index
        else:
            orch.knowledge_index = old_knowledge
        knowledge.cleanup()


async def registered_chat(orch, websocket, message, chat_id, *, user_id, dispatch=None, **kwargs):
    from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT
    from verification.drivers.fixture_admission import admitted_registered_turn

    with registered_human_turn(orch, websocket, message, chat_id, user_id=user_id) as (pending, frame):
        async with admitted_registered_turn(orch, websocket, frame=frame, human_request=pending) as context:
            token = _CONNECTION_OPERATION_CONTEXT.set(context)
            try:
                if dispatch is not None:
                    return await dispatch(websocket, message, chat_id, user_id=user_id, **kwargs)
                return await orch.handle_chat_message(
                    websocket, message, chat_id, user_id=user_id, operation_context=context, **kwargs,
                )
            finally:
                _CONNECTION_OPERATION_CONTEXT.reset(token)
