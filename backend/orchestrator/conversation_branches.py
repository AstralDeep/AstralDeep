"""Applies the owner's chat_fork, chat_regenerate, chat_branch_select and revised-turn
chat_message actions through HistoryManager's branch operations. Every action is
owner-checked and audited with its parent message ids. Branch actions refuse fail-closed
when the pinned AstralPlane revision does not carry the conversation-branch contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from orchestrator.history import (
    BRANCH_SUMMARY_PREFIX,
    ConversationBranchUnavailable,
    ConversationNotFound,
    ConversationSnapshotInvalid,
)

logger = logging.getLogger("ConversationBranches")

UNAVAILABLE_MESSAGE = "Conversation branches are not available on this server yet."
MESSAGES = {
    "invalid_request": "That chat request is not valid.",
    "chat_not_found": "That chat is not available.",
    "branch_unavailable": UNAVAILABLE_MESSAGE,
    "branch_failed": "That branch action could not be completed. Try again.",
}
_ACTION_AUDIT = {
    "chat_fork": "chat.forked",
    "chat_regenerate": "chat.regenerated",
    "chat_branch_select": "chat.branch_switched",
    "chat_message": "chat.branch_created",
}
_TURN_ACTIONS = frozenset({"chat_fork", "chat_regenerate", "chat_branch_select"})
_TURN_PAYLOAD_FIELDS = {
    "chat_fork": {"chat_id", "turn_id", "summarize_departed"},
    "chat_regenerate": {"chat_id", "turn_id", "summarize_departed"},
    "chat_branch_select": {"chat_id", "turn_id", "target_turn_id", "summarize_departed"},
}
_REVISED_PAYLOAD_FIELDS = {
    "message",
    "chat_id",
    "revised_turn_id",
    "summarize_departed",
    "draft_agent_id",
    "selected_tools",
    "async_mode",
    "display_message",
}


def _turn_id(value: Any) -> Any:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value).strip()
    return value if text.isdigit() and int(text) > 0 else None


async def _refuse(
    orch,
    websocket,
    action: str,
    code: str,
    submission_id: Any = None,
) -> None:
    await orch._safe_send(
        websocket,
        json.dumps(
            {
                "type": "error",
                "submission_id": str(submission_id or ""),
                "accepted": False,
                "code": "operation_failed",
                "message": MESSAGES[code],
                "action": action,
                "retryable": False,
                "retry_after_ms": None,
            }
        ),
    )


async def _audit(
    user_id: str,
    action: str,
    chat_id: str,
    *,
    outcome: str = "success",
    detail: str | None = None,
    **meta: Any,
) -> None:
    from audit.recorder import get_recorder, make_correlation_id
    from audit.schemas import AuditEventCreate

    recorder = get_recorder()
    if recorder is None:
        return
    try:
        await recorder.record(AuditEventCreate(
            actor_user_id=user_id, auth_principal=user_id, event_class="conversation",
            action_type=_ACTION_AUDIT[action], description=_ACTION_AUDIT[action],
            conversation_id=chat_id, correlation_id=make_correlation_id(), outcome=outcome,
            outcome_detail=detail,
            inputs_meta={key: value for key, value in meta.items() if value is not None},
            started_at=datetime.now(timezone.utc)))
    except Exception:  # noqa: BLE001
        logger.warning("conversation branch audit failed (%s)", action, exc_info=True)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _ancestor_ids(records: Any, message_id: int) -> set[int]:
    parents = {record.message_id: record.parent_message_id for record in records}
    chain = set()
    cursor = message_id
    while cursor is not None and cursor in parents and cursor not in chain:
        chain.add(cursor)
        cursor = parents[cursor]
    return chain


def _departed(view: dict, keep_ids: set[int]) -> tuple:
    return tuple(
        record for record in view["visible"] if record.message_id not in keep_ids
    )


async def summarize_departed_messages(
    orch,
    websocket,
    *,
    chat_id: str,
    user_id: str,
    departed,
) -> str | None:
    from llm_config.client_factory import create_completion
    from personalization.phi_policy import redaction_gate

    transcript = []
    for record in departed:
        if record.role not in ("user", "assistant"):
            continue
        text = _message_text(record.content).strip()
        if text:
            transcript.append(f"{record.role}: {text[:600]}")
    transcript = transcript[-12:]
    if not transcript:
        return None
    actor_user_id, auth_principal = orch._llm_audit_principals(websocket)
    feature = "branch_summary"
    resolved = None
    source = None
    try:
        client, source, resolved = await orch._resolve_llm_client_for(websocket)
        request = {"model": resolved.model, "messages": [
            {"role": "system", "content": (
                "Summarize the conversation turns the user is leaving behind in at most "
                "three sentences, as background context for a fresh start. Reply with the "
                "summary alone.")},
            {"role": "user", "content": "\n".join(transcript)[:6000]},
        ], "max_tokens": 300}
        response = await orch._context_model_call(
            websocket, chat_id, feature, resolved.model,
            lambda: create_completion(client, **request),
            request=request, base_url=getattr(resolved, "base_url", None),
            provider_capture=getattr(client, "_evidence_provider_capture", None),
        )
        usage = getattr(response, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        await orch._record_llm_call(
            orch.audit_recorder,
            actor_user_id=actor_user_id,
            auth_principal=auth_principal,
            feature=feature,
            credential_source=source,
            resolved=resolved,
            total_tokens=total_tokens,
            outcome="success",
        )
        if source == orch._CredentialSource.USER and websocket is not None:
            await orch._emit_llm_usage_report(
                websocket, feature=feature, model=resolved.model,
                usage=usage, outcome="success",
            )
        from shared.llm_text import strip_reasoning_markup

        summary = (strip_reasoning_markup(response.choices[0].message.content) or "").strip()
        if not summary:
            return None
        gate = await asyncio.to_thread(redaction_gate, user_id, orch)
        if gate is not None:
            summary, _changed = await asyncio.to_thread(
                gate.redact_for_storage, summary[:2000]
            )
        return summary[:1200] or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("branch summary call failed: %s", exc)
        try:
            await orch._record_llm_call(
                orch.audit_recorder,
                actor_user_id=actor_user_id,
                auth_principal=auth_principal,
                feature=feature,
                credential_source=source,
                resolved=resolved,
                total_tokens=None,
                outcome="failure",
            )
        except Exception:  # noqa: BLE001
            logger.debug("branch summary failure audit failed", exc_info=True)
        return None


async def _emit_hydration(orch, websocket, *, user_id: str, chat_id: str) -> None:
    from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT

    operation_context = orch._conversation_authority(
        _CONNECTION_OPERATION_CONTEXT.get(),
        websocket,
    )
    if operation_context is None:
        return
    operation, _owner, _fence = operation_context
    try:
        await orch._emit_hydration_snapshot(
            websocket,
            chat_id=chat_id,
            user_id=user_id,
            connection_generation=operation.connection_generation,
            request_generation=operation.request_generation,
        )
    except Exception:  # noqa: BLE001
        logger.warning("branch snapshot delivery failed", exc_info=True)


async def handle_branch_action(
    orch,
    websocket,
    user_id: str,
    action: str,
    payload: Any,
    submission_id: Any = None,
) -> None:
    chat_id = payload.get("chat_id") if isinstance(payload, dict) else None
    turn_id = _turn_id(payload.get("turn_id") if isinstance(payload, dict) else None)
    target_id = _turn_id(
        payload.get("target_turn_id") if isinstance(payload, dict) else None
    )
    summarize = (
        isinstance(payload, dict) and payload.get("summarize_departed") is True
    )
    if (
        not isinstance(payload, dict)
        or set(payload) - _TURN_PAYLOAD_FIELDS[action]
        or not isinstance(chat_id, str)
        or not 0 < len(chat_id) <= 256
        or turn_id is None
        or (action == "chat_branch_select" and target_id is None)
    ):
        await _refuse(orch, websocket, action, "invalid_request", submission_id)
        return
    history = orch.history
    try:
        record = await asyncio.to_thread(
            history.get_conversation_record, chat_id, user_id
        )
        if record is None:
            await _audit(
                user_id, action, chat_id, outcome="failure", detail="owner_denied",
                turn_id=str(turn_id),
            )
            await _refuse(orch, websocket, action, "chat_not_found", submission_id)
            return
        view = await asyncio.to_thread(history.branch_active_view, chat_id, user_id)
        if view is None:
            raise ConversationBranchUnavailable(
                "the pinned AstralPlane revision does not carry conversation branches"
            )
        if action == "chat_fork":
            await _handle_fork(
                orch, websocket, history, user_id=user_id, chat_id=chat_id,
                view=view, turn_id=int(turn_id), summarize=summarize,
                submission_id=submission_id,
            )
            return
        if action == "chat_regenerate":
            await _handle_regenerate(
                orch, websocket, history, user_id=user_id, chat_id=chat_id,
                view=view, turn_id=int(turn_id), summarize=summarize,
            )
            return
        await _handle_branch_select(
            orch, websocket, history, user_id=user_id, chat_id=chat_id,
            view=view, turn_id=int(turn_id), target_id=int(target_id),
            summarize=summarize,
        )
    except ConversationBranchUnavailable:
        await _audit(
            user_id, action, chat_id, outcome="failure",
            detail="branch_support_unavailable", turn_id=str(turn_id),
        )
        await _refuse(orch, websocket, action, "branch_unavailable", submission_id)
    except ConversationNotFound:
        await _audit(
            user_id, action, chat_id, outcome="failure", detail="owner_denied",
            turn_id=str(turn_id),
        )
        await _refuse(orch, websocket, action, "chat_not_found", submission_id)
    except ConversationSnapshotInvalid as exc:
        logger.info("branch action refused: %s", exc)
        await _refuse(orch, websocket, action, "invalid_request", submission_id)
    except Exception:  # noqa: BLE001
        logger.warning("conversation branch action failed (%s)", action, exc_info=True)
        await _refuse(orch, websocket, action, "branch_failed", submission_id)


def _require_on_path(view: dict, message_id: int):
    for position, record in enumerate(view["path"]):
        if record.message_id == message_id:
            return position, record
    raise ConversationSnapshotInvalid("message is not on the active branch")


async def _handle_fork(
    orch,
    websocket,
    history,
    *,
    user_id: str,
    chat_id: str,
    view: dict,
    turn_id: int,
    summarize: bool,
    submission_id: Any,
) -> None:
    position, _record = _require_on_path(view, turn_id)
    departed = tuple(view["path"][position + 1:])
    summary = None
    if summarize:
        summary = await summarize_departed_messages(
            orch, websocket, chat_id=chat_id, user_id=user_id, departed=departed
        )
    new_chat_id = await asyncio.to_thread(
        history.fork_chat, chat_id, user_id, turn_id, summary=summary
    )
    await _audit(
        user_id, "chat_fork", chat_id, source_message_id=str(turn_id),
        forked_chat_id=new_chat_id, summarized=bool(summary),
    )
    await orch._safe_send(websocket, json.dumps({
        "type": "chat_created",
        "payload": {
            "chat_id": new_chat_id,
            "from_fork": True,
            "source_chat_id": chat_id,
        },
    }))
    await orch._broadcast_user_history(user_id=user_id)
    await _emit_hydration(orch, websocket, user_id=user_id, chat_id=new_chat_id)


async def _handle_regenerate(
    orch,
    websocket,
    history,
    *,
    user_id: str,
    chat_id: str,
    view: dict,
    turn_id: int,
    summarize: bool,
) -> None:
    _position, reply = _require_on_path(view, turn_id)
    if reply.role == "user":
        raise ConversationSnapshotInvalid("only assistant replies can be regenerated")
    parent_id = reply.parent_message_id
    if parent_id is None:
        raise ConversationSnapshotInvalid("regenerated reply has no parent turn")
    parent = next(
        (record for record in view["visible"] if record.message_id == parent_id), None
    )
    if parent is None:
        raise ConversationSnapshotInvalid("regenerated reply has no parent turn")
    summary = None
    if summarize:
        departed = _departed(view, _ancestor_ids(view["visible"], parent_id))
        summary = await summarize_departed_messages(
            orch, websocket, chat_id=chat_id, user_id=user_id, departed=departed
        )
    prepared = await asyncio.to_thread(
        history.prepare_regenerate, chat_id, user_id, turn_id
    )
    if summary is not None:
        await asyncio.to_thread(
            history.add_message,
            chat_id,
            "user",
            f"{BRANCH_SUMMARY_PREFIX} {summary}",
            user_id=user_id,
        )
    await _audit(
        user_id, "chat_regenerate", chat_id,
        regenerated_message_id=str(turn_id),
        parent_message_id=str(prepared["head_message_id"]),
        summarized=bool(summary),
    )
    message_text = _message_text(parent.content)
    await orch._serialized_chat(
        websocket, message_text, chat_id, message_text,
        user_id=user_id,
        persisted_turn={"message_id": parent_id},
    )


async def _handle_branch_select(
    orch,
    websocket,
    history,
    *,
    user_id: str,
    chat_id: str,
    view: dict,
    turn_id: int,
    target_id: int,
    summarize: bool,
) -> None:
    _position, _turn = _require_on_path(view, turn_id)
    target = next(
        (record for record in view["visible"] if record.message_id == target_id), None
    )
    if target is None:
        raise ConversationSnapshotInvalid("branch target message was not found")
    summary = None
    if summarize:
        departed = _departed(view, _ancestor_ids(view["visible"], target_id))
        summary = await summarize_departed_messages(
            orch, websocket, chat_id=chat_id, user_id=user_id, departed=departed
        )
    await asyncio.to_thread(
        history.switch_branch, chat_id, user_id, target_id, summary=summary
    )
    await _audit(
        user_id, "chat_branch_select", chat_id, turn_id=str(turn_id),
        target_message_id=str(target_id), summarized=bool(summary),
    )
    await _emit_hydration(orch, websocket, user_id=user_id, chat_id=chat_id)


async def dispatch_revised_message(
    orch,
    websocket,
    *,
    user_id: str,
    payload: Any,
    submission_id: Any = None,
) -> None:
    action = "chat_message"
    chat_id = payload.get("chat_id") if isinstance(payload, dict) else None
    message = payload.get("message") if isinstance(payload, dict) else None
    revised_turn_id = _turn_id(
        payload.get("revised_turn_id") if isinstance(payload, dict) else None
    )
    summarize = (
        isinstance(payload, dict) and payload.get("summarize_departed") is True
    )
    if (
        not isinstance(payload, dict)
        or set(payload) - _REVISED_PAYLOAD_FIELDS
        or not isinstance(chat_id, str)
        or not 0 < len(chat_id) <= 256
        or revised_turn_id is None
        or not isinstance(message, str)
        or not message.strip()
    ):
        await _refuse(orch, websocket, action, "invalid_request", submission_id)
        return
    history = orch.history
    try:
        record = await asyncio.to_thread(
            history.get_conversation_record, chat_id, user_id
        )
        if record is None:
            await _audit(
                user_id, action, chat_id, outcome="failure", detail="owner_denied",
                revised_message_id=str(revised_turn_id),
            )
            await _refuse(orch, websocket, action, "chat_not_found", submission_id)
            return
        view = await asyncio.to_thread(history.branch_active_view, chat_id, user_id)
        if view is None:
            raise ConversationBranchUnavailable(
                "the pinned AstralPlane revision does not carry conversation branches"
            )
        _position, target = _require_on_path(view, int(revised_turn_id))
        if target.role != "user":
            raise ConversationSnapshotInvalid("edited message must be a user turn")
        summary = None
        if summarize:
            departed = _departed(view, _ancestor_ids(view["visible"], target.message_id))
            summary = await summarize_departed_messages(
                orch, websocket, chat_id=chat_id, user_id=user_id, departed=departed
            )
        result = await asyncio.to_thread(
            history.edit_and_resend,
            chat_id,
            user_id,
            revised_turn_id,
            message,
        )
        if summary is not None:
            await asyncio.to_thread(
                history.add_message,
                chat_id,
                "user",
                f"{BRANCH_SUMMARY_PREFIX} {summary}",
                user_id=user_id,
            )
        await _audit(
            user_id, action, chat_id, revised_message_id=str(revised_turn_id),
            replacement_message_id=str(result["message_id"]),
            summarized=bool(summary),
        )
        await orch._serialized_chat(
            websocket, message, chat_id, message,
            user_id=user_id,
            persisted_turn={"message_id": int(result["message_id"])},
        )
    except ConversationBranchUnavailable:
        await _audit(
            user_id, action, chat_id, outcome="failure",
            detail="branch_support_unavailable", revised_message_id=str(revised_turn_id),
        )
        await _refuse(orch, websocket, action, "branch_unavailable", submission_id)
    except ConversationNotFound:
        await _audit(
            user_id, action, chat_id, outcome="failure", detail="owner_denied",
            revised_message_id=str(revised_turn_id),
        )
        await _refuse(orch, websocket, action, "chat_not_found", submission_id)
    except ConversationSnapshotInvalid as exc:
        logger.info("revised message refused: %s", exc)
        await _refuse(orch, websocket, action, "invalid_request", submission_id)
    except Exception:  # noqa: BLE001
        logger.warning("revised message dispatch failed", exc_info=True)
        await _refuse(orch, websocket, action, "branch_failed", submission_id)
