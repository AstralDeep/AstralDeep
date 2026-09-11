"""Attended HITL decisions bound to one server-held call, never client args.

Requests live for fifteen minutes in the owning process. Restart or eviction
expires them closed; sensitive arguments are never persisted in UI payloads.
Approval re-enters the complete dispatcher and only satisfies this HITL gate.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from astralprims import Alert, Button, Card, CodeBlock, Text

from orchestrator.hitl import confirmation_request
from shared.protocol import MCPResponse

logger = logging.getLogger(__name__)
TTL_SECONDS = 900


@dataclass(frozen=True)
class PendingCall:
    request_id: str
    owner: str
    chat: str
    agent: str
    tool: str
    arguments: str
    risks: tuple[str, ...]
    expires: float


@dataclass
class Approval:
    orchestrator: object
    call: PendingCall
    consumed: bool = False
    effect_started: bool = False


_APPROVAL = contextvars.ContextVar("hitl_approval", default=None)
_SERVER_ARGUMENTS = frozenset({"_credentials", "_credentials_encrypted", "_session_llm_credentials",
                               "_delegation_token", "_cap_job_id"})
_DESTINATION_FIELDS = frozenset({"to", "cc", "bcc", "recipient", "recipients", "email", "to_email",
                                 "phone", "destination"})
_SECRET_MARKERS = re.compile(r"(?i)(?:api[_ -]?key|access[_ -]?token|secret|password|authorization|"
                             r"bearer |credential|-----BEGIN|\bsk-[\w-]{12,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+)")
_EMAIL_DESTINATION = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_DESTINATION = re.compile(r"\+?[0-9(). -]{10,24}")


def _routing_address(key, value) -> bool:
    if key.lower() not in _DESTINATION_FIELDS or not isinstance(value, str) or len(value) > 320:
        return False
    return bool(_EMAIL_DESTINATION.fullmatch(value) or (
        _PHONE_DESTINATION.fullmatch(value)
        and 10 <= sum(character.isdigit() for character in value) <= 15))


def _arguments(args) -> str:
    if not isinstance(args, dict):
        raise ValueError("confirmation arguments must be an object")
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("confirmation arguments exceed limit")
    return encoded


def _pending(orch) -> dict[str, PendingCall]:
    current = getattr(orch, "_hitl_pending_calls", {})
    current = {key: call for key, call in current.items() if call.expires > time.monotonic()}
    orch._hitl_pending_calls = current
    return current


def _attended(orch, websocket, owner: str) -> bool:
    from orchestrator.async_tasks import VirtualWebSocket

    if websocket is None or isinstance(websocket, VirtualWebSocket):
        return False
    claims = getattr(orch, "ui_sessions", {}).get(websocket)
    return (isinstance(claims, dict) and claims.get("sub") == owner
            and not claims.get("machine_class") and claims.get("_invocation_channel") != "mcp")


def _current_session(orch, websocket, call: PendingCall) -> bool:
    return (_attended(orch, websocket, call.owner)
            and getattr(orch, "_ws_active_chat", {}).get(id(websocket)) == call.chat
            and call.expires > time.monotonic())


def matching_approval(orch, owner, chat, agent, tool, args) -> bool:
    """A click is expressed intent only for its exact owner/chat/tool/arguments."""
    approval = _APPROVAL.get()
    if not isinstance(approval, Approval) or approval.orchestrator is not orch or approval.consumed:
        return False
    call = approval.call
    return (call.expires > time.monotonic()
            and (call.owner, call.chat, call.agent, call.tool) == (owner, chat, agent, tool)
            and call.arguments == _arguments(args))


def approved_call(orch, owner, chat, agent, tool) -> bool:
    """Whether this invocation is the exact target of an attended approval."""
    approval = _APPROVAL.get()
    return (isinstance(approval, Approval) and approval.orchestrator is orch
            and (approval.call.owner, approval.call.chat, approval.call.agent, approval.call.tool)
            == (owner, chat, agent, tool))


def effect_refusal(orch, websocket, owner, chat, agent, tool, args, *, start=False) -> MCPResponse | None:
    """Revalidate the reviewed effect after all awaits, once per physical send."""
    if not approved_call(orch, owner, chat, agent, tool):
        return None
    approval = _APPROVAL.get()
    if (not approval.consumed or approval.effect_started
            or not _current_session(orch, websocket, approval.call)):
        return _refused("Approval is no longer valid for this session or was already used. Reissue the action.")
    original = json.loads(approval.call.arguments)
    compared = {key: value for key, value in args.items() if key in original or key not in _SERVER_ARGUMENTS}
    for key, expected in (("user_id", owner), ("session_id", chat)):
        if key not in original and compared.get(key) == expected:
            compared.pop(key)
    if _arguments(compared) != approval.call.arguments:
        return _refused("The action arguments changed after review. Reissue the action for a new approval.")
    if start:
        approval.effect_started = True
    return None


async def _audit(orch, call: PendingCall, transition: str) -> None:
    from audit.recorder import now_utc
    from audit.schemas import AuditEventCreate

    try:
        await orch.audit_recorder.record(AuditEventCreate(
            actor_user_id=call.owner, auth_principal=call.owner,
            event_class="agent_tool_call", action_type=f"hitl.{transition}",
            description=f"{transition}: {call.tool}", correlation_id=call.request_id,
            conversation_id=call.chat, outcome="success",
            inputs_meta={"agent_id": call.agent, "tool": call.tool, "risks": list(call.risks)},
            started_at=now_utc(), completed_at=now_utc(),
        ))
    except Exception:
        logger.debug("HITL audit unavailable", exc_info=True)


def _refused(message: str) -> MCPResponse:
    return MCPResponse(error={"message": message, "retryable": False})


def _review_summary(args: dict) -> str:
    """Describe the destination and fields without disclosing their contents."""
    fields = ", ".join(str(key)[:64] for key in args if not str(key).startswith("_"))[:512]
    description = f"Fields supplied: {fields or 'none'}."
    url = args.get("url")
    if isinstance(url, str):
        try:
            host = urlsplit(url).hostname
            if host:
                description = f"Destination host: {host[:253]}. {description}"
        except ValueError:
            description = f"Destination URL needs review. {description}"
    return description


def _review_arguments(args: dict) -> tuple[str, bool]:
    """A complete bounded preview, or a redacted explanation with no approval.

    The existing PHI redactor masks identifiers; credential markers and fields
    are additionally withheld. If any value is hidden/truncated, the user cannot
    review the exact effect, so the caller must not offer blind approval.
    """
    from orchestrator.hitl import _SENSITIVE_INPUT, sensitive_url
    from personalization.phi_gate import get_phi_gate
    from shared.phi_redactor import PHI_FIELD_PATTERNS, redact

    reviewable = True
    visible_strings = []

    def walk(value, key=""):
        nonlocal reviewable
        destination = _routing_address(key, value)
        sensitive_key = _SECRET_MARKERS.search(key) or any(
            marker in key.lower() for marker in ("password", "credential", "authorization", "cookie", "private_key"))
        if sensitive_key or (not destination and key not in {"url", "filename", "file_name", "hostname"}
                             and any(marker in key.lower() for marker in PHI_FIELD_PATTERNS)):
            reviewable = False
            return "[REDACTED — review unavailable]"
        if isinstance(value, dict):
            return {name: walk(child, name) for name, child in value.items()}
        if isinstance(value, list):
            return [walk(child, key) for child in value]
        if isinstance(value, str):
            decoded = unquote(unquote(value))
            if _SECRET_MARKERS.search(decoded) or (not destination and _SENSITIVE_INPUT.search(decoded)):
                reviewable = False
                return "[REDACTED — review unavailable]"
            if key == "url":
                try:
                    url = urlsplit(decoded)
                    if sensitive_url(url):
                        reviewable = False
                        return "[REDACTED URL credentials]"
                except ValueError:
                    reviewable = False
                    return "[Invalid URL — review unavailable]"
            cleaned, truncated = (value, False) if destination else redact(value)
            if truncated or cleaned != value:
                reviewable = False
            if not destination:
                visible_strings.append(value)
            return cleaned
        return value

    preview = json.dumps(walk(args), indent=2, ensure_ascii=False)
    if len(preview) > 4096:
        return "[Arguments exceed the complete review limit. Use a smaller action.]", False
    if reviewable and visible_strings:
        try:
            if get_phi_gate().contains_phi("\n".join(visible_strings)):
                return "[Private identifiers cannot be shown in an approval preview.]", False
        except Exception:
            return "[Privacy review unavailable. Reissue the action when it is available.]", False
    return preview, reviewable


async def evaluate(orch, websocket, owner, chat, agent, tool, args, risks) -> MCPResponse | None:
    """Consume the exact approved call or return a persistent actionable card."""
    try:
        encoded = _arguments(args)
        if matching_approval(orch, owner, chat, agent, tool, args):
            approval = _APPROVAL.get()
            if not _current_session(orch, websocket, approval.call):
                return _refused("This confirmation is no longer valid for the active session.")
            if approval.call.risks == tuple(sorted(risks)):
                approval.consumed = True
                await _audit(orch, approval.call, "consumed")
                if not _current_session(orch, websocket, approval.call):
                    return _refused("This confirmation expired or the active session changed. Reissue the action.")
                return None
        if not chat or not _attended(orch, websocket, owner):
            return _refused("Confirmation requires an interactive signed-in conversation. Reissue the action there.")
        # Resolve owner-scoped attachment aliases before review. Re-entry maps
        # these concrete targets idempotently, so benign path resolution cannot
        # silently change the approved effect or cause an endless review loop.
        try:
            args = await asyncio.to_thread(orch._map_file_paths, chat, json.loads(encoded), user_id=owner)
            encoded = _arguments(args)
        except Exception:
            return _refused("Review unavailable: action targets could not be resolved. Reissue the action when they are available.")
        preview, reviewable = await asyncio.to_thread(_review_arguments, args)
        if not reviewable:
            return _refused("Review unavailable: this action includes sensitive or oversized arguments that cannot be "
                            "shown completely. Remove private identifiers or credentials, or use a smaller action.")
        if (not _attended(orch, websocket, owner)
                or getattr(orch, "_ws_active_chat", {}).get(id(websocket)) != chat):
            return _refused("This confirmation is no longer valid for the active session.")
        pending = _pending(orch)
        call = next((item for item in pending.values() if
                     (item.owner, item.chat, item.agent, item.tool, item.arguments, item.risks)
                     == (owner, chat, agent, tool, encoded, tuple(sorted(risks)))), None)
        if call is None:
            if len(pending) >= 128 or sum(item.owner == owner for item in pending.values()) >= 32:
                return _refused("Too many pending confirmations. Resolve an existing request and retry.")
            call = PendingCall(secrets.token_urlsafe(24), owner, chat, agent, tool,
                               encoded, tuple(sorted(risks)), time.monotonic() + TTL_SECONDS)
            pending[call.request_id] = call
            await _audit(orch, call, "proposed")
        if not _current_session(orch, websocket, call):
            getattr(orch, "_hitl_pending_calls", {}).pop(call.request_id, None)
            return _refused("This confirmation is no longer valid for the active conversation.")
        summary = confirmation_request(tool, list(call.risks)).summary
        card = Card(title="Review action", id=f"hitl_{call.request_id}", content=[
            Text(content=f"Tool: {tool} · Agent: {agent}", variant="body"),
            Text(content=summary, variant="body"),
            Text(content=_review_summary(args), variant="body"),
            CodeBlock(code=preview, language="json"),
            Text(content="Approve this exact action once, or decline. This request expires in 15 minutes. "
                         "Privacy rules and current permissions still apply.", variant="caption"),
            Button(label="Approve", action="authorize_action",
                   payload={"hitl_request_id": call.request_id, "decision": "approve"}),
            Button(label="Decline", action="authorize_action", variant="secondary",
                   payload={"hitl_request_id": call.request_id, "decision": "decline"}),
        ]).to_dict()
        return MCPResponse(result={"_data": {"status": "confirmation_required",
                           "message": "An approval card is available. End this turn and wait for the user's decision; "
                                      "do not retry the action or ask again in text."}}, ui_components=[card])
    except (TypeError, ValueError):
        return _refused("Confirmation could not be prepared. Correct the action arguments and retry.")


async def _finish_card(orch, websocket, call, title, message, components=None):
    """Retire the same workspace card across devices; an old click stays invalid."""
    try:
        cid = f"hitl_{call.request_id}"
        component = Card(id=cid, title=title, content=[Text(content=message)]).to_dict()

        async def mutation():
            ops = await orch.workspace.aupsert(call.chat, call.owner, [component], force_component_id=cid)
            if components:
                ops += await orch.workspace.aupsert(call.chat, call.owner, components)
            return ops

        ops = await orch.run_detached_conversation_mutation(chat_id=call.chat, user_id=call.owner, mutation=mutation)
        if ops:
            await orch.send_ui_upsert(None, call.chat, call.owner, ops)
    except Exception:
        logger.debug("HITL card replacement unavailable", exc_info=True)
    await orch.send_ui_render(websocket, [Alert(message=message, variant="info").to_dict()], target="chat")


async def handle_decision(orch, websocket, owner, payload) -> None:
    """Accept only an opaque request ID and a decision from its attended owner."""
    pending = _pending(orch)
    request_id = payload.get("hitl_request_id") if isinstance(payload, dict) else None
    call = pending.get(request_id) if isinstance(request_id, str) else None
    if (call is None or call.owner != owner or not _attended(orch, websocket, owner)
            or getattr(orch, "_ws_active_chat", {}).get(id(websocket)) != call.chat
            or set(payload) != {"hitl_request_id", "decision"}
            or payload.get("decision") not in ("approve", "decline")):
        await orch.send_ui_render(websocket, [Alert(message="This confirmation is unavailable in this conversation. "
            "It may have expired or already been handled.", variant="warning").to_dict()], target="chat")
        return
    # Pop before any await: concurrent clicks cannot both authorize the request.
    pending.pop(request_id)
    try:
        owned_chat = await asyncio.to_thread(orch.history.get_chat, call.chat, user_id=owner)
    except Exception:
        owned_chat = None
    if not owned_chat:
        await _finish_card(orch, websocket, call, "Unavailable", "Conversation access could not be verified. Reissue the action.")
        return
    if not _current_session(orch, websocket, call):
        await _finish_card(orch, websocket, call, "Unavailable", "The session changed or this confirmation expired. Reissue the action.")
        return
    if payload["decision"] == "decline":
        await _audit(orch, call, "declined")
        await _finish_card(orch, websocket, call, "Declined", "Declined — the action was not run.")
        return
    await _audit(orch, call, "approved")
    if not _current_session(orch, websocket, call):
        await _finish_card(orch, websocket, call, "Unavailable", "The session changed or this confirmation expired. Reissue the action.")
        return
    token = _APPROVAL.set(Approval(orch, call))
    try:
        tool_call = SimpleNamespace(id=f"hitl-{call.request_id}", function=SimpleNamespace(
            name=call.tool, arguments=call.arguments))
        response = await orch.execute_single_tool(websocket, tool_call, {call.tool: call.agent}, call.chat, user_id=owner)
    except Exception:
        logger.exception("Approved HITL dispatch failed")
        await _finish_card(orch, websocket, call, "Failed", "The approved action could not complete. Reissue it to retry.")
        return
    finally:
        _APPROVAL.reset(token)
    failed = response is None or bool(response.error)
    data = response.result.get("_data", {}) if response and isinstance(response.result, dict) else {}
    waiting = isinstance(data, dict) and data.get("status") == "confirmation_required"
    if waiting:
        await _finish_card(orch, websocket, call, "Further review needed",
                           "The action still needs approval. Review the new card before it can run.",
                           response.ui_components)
        return
    await _finish_card(orch, websocket, call, "Blocked" if failed else "Approved",
                       "The approved action was blocked or failed. See the tool message for details." if failed
                       else "Approval used — the action was sent through the normal permission and privacy checks.",
                       response.ui_components if response and not failed else None)
