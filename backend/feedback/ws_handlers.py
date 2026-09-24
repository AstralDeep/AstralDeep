"""WebSocket handlers for component_feedback / feedback_retract / feedback_amend,
invoked from the orchestrator's ui_event dispatch loop with an already-authenticated
user_id. Delegate to feedback/recorder.py and ack over the same socket.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from pydantic import ValidationError

from .recorder import EditWindowExpired, FeedbackNotFound, Recorder
from .schemas import FeedbackAmendRequest, FeedbackSubmitRequest

logger = logging.getLogger("Feedback.WS")

SafeSend = Callable[[Any, str], Awaitable[None]]


async def _send(safe_send: SafeSend, websocket: Any, action: str, payload: Dict[str, Any]) -> None:
    msg = {"type": "ui_event", "action": action, "payload": payload}
    await safe_send(websocket, json.dumps(msg))


async def _send_error(safe_send: SafeSend, websocket: Any, code: str, message: str) -> None:
    await _send(safe_send, websocket, "component_feedback_error", {
        "code": code, "message": message,
    })


async def handle_component_feedback(
    *,
    safe_send: SafeSend,
    websocket: Any,
    payload: Dict[str, Any],
    actor_user_id: str,
    auth_principal: str,
    recorder: Recorder,
    conversation_id: Optional[str] = None,
) -> None:
    try:
        req = FeedbackSubmitRequest(**payload)
    except ValidationError as exc:
        await _send_error(safe_send, websocket, "INVALID_INPUT", str(exc))
        return
    except Exception as exc:
        await _send_error(safe_send, websocket, "INVALID_INPUT", str(exc))
        return

    try:
        result = await recorder.submit(
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            conversation_id=conversation_id,
            correlation_id=req.correlation_id,
            source_agent=req.source_agent,
            source_tool=req.source_tool,
            component_id=req.component_id,
            sentiment=req.sentiment,
            category=req.category,
            comment=req.comment,
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("component_feedback submit failed: %s", exc)
        await _send_error(safe_send, websocket, "INVALID_INPUT", "submit failed")
        return

    await _send(safe_send, websocket, "component_feedback_ack", {
        "feedback_id": str(result.feedback.id),
        "status": result.status,
        "deduped": result.deduped,
    })


async def handle_feedback_retract(
    *,
    safe_send: SafeSend,
    websocket: Any,
    payload: Dict[str, Any],
    actor_user_id: str,
    auth_principal: str,
    recorder: Recorder,
) -> None:
    feedback_id = payload.get("feedback_id")
    if not feedback_id or not isinstance(feedback_id, str):
        await _send_error(safe_send, websocket, "INVALID_INPUT", "feedback_id is required")
        return
    try:
        updated = await recorder.retract(actor_user_id, auth_principal, feedback_id)
    except FeedbackNotFound:
        await _send_error(safe_send, websocket, "NOT_FOUND", "feedback not found")
        return
    except EditWindowExpired:
        await _send_error(safe_send, websocket, "EDIT_WINDOW_EXPIRED",
                            "retract window of 24 hours has expired")
        return
    except Exception as exc:  # pragma: no cover
        logger.exception("feedback_retract failed: %s", exc)
        await _send_error(safe_send, websocket, "INVALID_INPUT", "retract failed")
        return

    await _send(safe_send, websocket, "feedback_retract_ack", {
        "feedback_id": str(updated.id),
        "status": "retracted",
    })


async def handle_feedback_amend(
    *,
    safe_send: SafeSend,
    websocket: Any,
    payload: Dict[str, Any],
    actor_user_id: str,
    auth_principal: str,
    recorder: Recorder,
) -> None:
    feedback_id = payload.get("feedback_id")
    if not feedback_id or not isinstance(feedback_id, str):
        await _send_error(safe_send, websocket, "INVALID_INPUT", "feedback_id is required")
        return
    fields = {k: v for k, v in payload.items() if k != "feedback_id"}
    comment_explicit = "comment" in fields
    try:
        req = FeedbackAmendRequest(**fields)
    except ValidationError as exc:
        await _send_error(safe_send, websocket, "INVALID_INPUT", str(exc))
        return

    try:
        new_row = await recorder.amend(
            actor_user_id, auth_principal, feedback_id,
            sentiment=req.sentiment,
            category=req.category,
            comment=req.comment,
            comment_explicit=comment_explicit,
        )
    except FeedbackNotFound:
        await _send_error(safe_send, websocket, "NOT_FOUND", "feedback not found")
        return
    except EditWindowExpired:
        await _send_error(safe_send, websocket, "EDIT_WINDOW_EXPIRED",
                            "amend window of 24 hours has expired")
        return
    except Exception as exc:  # pragma: no cover
        logger.exception("feedback_amend failed: %s", exc)
        await _send_error(safe_send, websocket, "INVALID_INPUT", "amend failed")
        return

    await _send(safe_send, websocket, "feedback_amend_ack", {
        "feedback_id": str(new_row.id),
        "prior_id": feedback_id,
        "status": "amended",
        "comment_safety": new_row.comment_safety,
    })
