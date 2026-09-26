"""Deliver device-adapted conversation snapshots without navigating or mutating history.
The orchestrator calls this only for explicitly scoped viewport hydration requests.
"""

from __future__ import annotations

import asyncio
import json

from orchestrator.work_admission import OwnerScope


async def refresh_viewport_snapshot(host, websocket, message, user_id, registration, authority, binding):
    payload = message.payload
    chat_id = payload.get("chat_id")
    connection = message.connection_generation or payload.get("connection_generation")
    request = message.request_generation or payload.get("request_generation")
    purpose = message.snapshot_purpose or payload.get("snapshot_purpose")
    revision = payload.get("base_render_revision")
    context = getattr(host, "_connection_contexts", {}).get(id(websocket))

    async def refuse(retryable=False):
        if host.ui_sessions.get(websocket) is not registration or host._get_user_id(websocket) != user_id:
            return
        await host._safe_send(websocket, json.dumps({
            "type": "error",
            "code": "viewport_snapshot_retryable" if retryable else "viewport_snapshot_rejected",
            "message": "Layout refresh is temporarily unavailable. Try again." if retryable
            else "Layout refresh was interrupted. Try again when the conversation is ready.",
            "chat_id": chat_id,
            "connection_generation": connection,
            "request_generation": request,
            "retryable": retryable,
        }))

    if (purpose != "hydration" or registration is None or authority is None or context is None or binding is None
            or host._canonical_uuid4(chat_id) is None
            or host._canonical_uuid4(connection) is None
            or host._canonical_uuid4(request) is None
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0):
        await refuse()
        return
    operation, owner, fence = authority

    def current_connection():
        return (
            host.ui_sessions.get(websocket) is registration
            and host._get_user_id(websocket) == user_id
            and getattr(host, "_connection_contexts", {}).get(id(websocket)) is context
            and context.registered and not context.closing
            and not getattr(context, "work_registrations_pending", 0)
            and str(context.connection_generation) == connection
            and owner.owner_scope is OwnerScope.CONNECTION
            and owner.connection_scope_id == context.connection_scope_id
            and str(operation.connection_generation) == connection
            and str(operation.request_generation) == request
            and operation.chat_id == chat_id
            and host._ws_active_chat.get(id(websocket)) == chat_id
        )

    def current():
        return (
            current_connection()
            and host._conversation_scopes.get(id(websocket)) is binding
            and binding.get("snapshot_completed") is True
            and binding.get("chat_id") == chat_id
            and binding.get("connection_generation") == connection
            and binding.get("request_generation") != request
            and binding.get("base_render_revision") == revision
            and not getattr(host, "_ws_timeline_mode", {}).get(id(websocket))
            and not any(
                key != operation.operation_id and (work.lane_complete is None or not work.lane_complete.done())
                for key, work in context.operations.items()
            )
        )

    def check_fence():
        with host.work_admission.fenced_transaction(fence):
            pass

    if not current():
        await refuse()
        return
    try:
        await asyncio.to_thread(check_fence)
        if not current():
            await refuse()
            return
        snapshot = await asyncio.to_thread(
            host.conversation_commits.build_snapshot,
            chat_id=chat_id, owner_user_id=user_id,
            connection_generation=connection, request_generation=request, snapshot_purpose="hydration",
        )
        await asyncio.to_thread(check_fence)
    except Exception:
        await refuse(retryable=True)
        return
    if not current():
        await refuse()
        return
    if snapshot["render_revision"] != revision:
        await refuse(retryable=snapshot["render_revision"] > revision)
        return
    canonical_canvas = snapshot["canvas"]["components"]
    try:
        snapshot = host._adapt_conversation_snapshot(websocket, snapshot, cache=False)
        encoded = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except Exception:
        await refuse(retryable=True)
        return
    refreshed = host._bind_conversation_scope(
        websocket, chat_id=chat_id, connection_generation=connection,
        request_generation=request, purpose="hydration",
        base_render_revision=snapshot["render_revision"],
    )
    delivered = False
    try:
        delivered = await host._safe_send(websocket, encoded)
    finally:
        if host._conversation_scopes.get(id(websocket)) is refreshed:
            if delivered and current_connection():
                refreshed["snapshot_completed"] = True
                host.rote.remember_components(websocket, canonical_canvas)
            else:
                if current_connection():
                    host._conversation_scopes[id(websocket)] = binding
                else:
                    host._conversation_scopes.pop(id(websocket), None)
