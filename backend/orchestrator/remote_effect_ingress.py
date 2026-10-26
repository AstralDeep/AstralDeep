"""Binds foreground remote approvals to server-held intent at socket admission.
Publishes qualified review cards through the existing conversation publication path.
"""

import hashlib
import json

from orchestrator import hitl_confirmation as hc
from orchestrator.remote_effect_execution import qualified_tool


def _qualified_call(call):
    return (type(call) is hc.PendingCall and qualified_tool(call.agent, call.tool)
        and "source_bound" in call.risks)


def pending_call(orch, websocket, message):
    if not isinstance(message, dict):
        return None
    payload = message.get("payload")
    if (message.get("type") != "ui_event" or message.get("action") != "authorize_action"
            or not isinstance(payload, dict) or payload.get("decision") != "approve"
            or set(payload) - {"hitl_request_id", "decision", "chat_id", "component_id",
                "submission_id", "request_generation", "connection_generation"}):
        return None
    request = payload.get("hitl_request_id")
    call = hc._pending(orch).get(request) if isinstance(request, str) else None
    if (not _qualified_call(call) or not hc._current_session(orch, websocket, call)
            or "chat_id" in payload and payload["chat_id"] != call.chat
            or "component_id" in payload and payload["component_id"] !=
                orch.workspace.resolve_identity({"id": f"hitl_{call.request_id}"})):
        return None
    return call


def bind_frame(orch, context, frame):
    payload = frame.parsed.get("payload")
    if (frame.action != "authorize_action" or not isinstance(payload, dict)
            or not isinstance(payload.get("hitl_request_id"), str)):
        return
    call = pending_call(orch, context.websocket, frame.parsed)
    if call is not None and frame.chat_id != call.chat:
        call = None
    original = next((work.frame.remote_effect_call for work in context.operations.values()
        if work.frame.submission_id == frame.submission_id and _qualified_call(work.frame.remote_effect_call)), None)
    payload = frame.parsed.get("payload", {})
    candidate = getattr(orch, "_hitl_pending_calls", {}).get(payload.get("hitl_request_id")) if isinstance(payload.get("hitl_request_id"), str) else None
    intent = candidate if _qualified_call(candidate) else original
    intent = intent or call
    if intent is None:
        return
    payload = {key: value for key, value in frame.parsed["payload"].items()
        if key not in {"submission_id", "request_generation", "connection_generation"}}
    frame.normalized_digest = hashlib.sha256(json.dumps({
        "base": frame.normalized_digest, "request": intent.request_id,
        "owner": intent.owner, "chat": intent.chat, "agent": intent.agent, "tool": intent.tool,
        "arguments": intent.arguments, "risks": intent.risks,
        "remote_proposal": intent.remote_proposal, "payload": payload,
        "intent_current": candidate is None or candidate is intent,
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if call is not None and context.registered and not context.closing and context.work_registrations_pending == 0:
        frame.remote_effect_call = call
        frame.foreground = True
        frame.operation_kind = "remote_effect_approval"


async def publish_review(orch, websocket, agent, tool, chat, owner, response):
    if (not qualified_tool(agent, tool) or response is None or response.error
            or not isinstance(response.result, dict)
            or not isinstance(response.result.get("_data"), dict)
            or response.result["_data"].get("status") != "confirmation_required"):
        return False
    components = response.ui_components or []
    calls = hc._pending(orch)
    cards = [card for card in components if isinstance(card, dict) and card.get("type") == "card"
        and any(card.get("id") == f"hitl_{call.request_id}"
            and (call.owner, call.chat, call.agent, call.tool) == (owner, chat, agent, tool)
            and "source_bound" in call.risks and hc._current_session(orch, websocket, call)
            for call in calls.values())]
    if len(cards) != 1 or len(components) != 1:
        return False

    async def mutation():
        return await orch.workspace.aupsert(chat, owner, cards)

    ops = await orch.run_detached_conversation_mutation(chat_id=chat, user_id=owner, mutation=mutation)
    if ops:
        await orch.send_ui_upsert(None, chat, owner, ops)
    return True
