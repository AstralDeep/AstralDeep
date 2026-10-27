"""Serializes explicit approval decisions into canonical owner/connection/chat-bound receipts.
Negotiated Mac clients consume these optional chat-status fields without inferring approval from a click.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time
from dataclasses import dataclass

_CURRENT = contextvars.ContextVar("explicit_approval_receipt", default=None)
MAXIMUM = 512
TTL = 1800
STATES = frozenset({"accepted", "refused", "expired", "cancelled", "error", "uncertain"})


@dataclass
class Receipt:
    orchestrator: object
    websocket: object
    connection: object
    generation: object
    owner: str
    chat: str
    request: str
    decision: str
    future: asyncio.Future
    expires: float
    state: str = "refused"
    message: str = "This confirmation is unavailable. Reissue the action if it is still needed."
    accepted: bool = False

    def current(self):
        claims = getattr(self.orchestrator, "ui_sessions", {}).get(self.websocket) or {}
        return (claims.get("sub") == self.owner and claims.get("_project_workspaces_v1") is True
                and self.orchestrator._connection_contexts.get(id(self.websocket)) is self.connection
                and self.connection.registered and not self.connection.closing
                and self.connection.connection_generation == self.generation)


def note(orch, request, state, message):
    receipt = _CURRENT.get()
    if (receipt is None or receipt.orchestrator is not orch or receipt.request != str(request)
            or state not in STATES):
        return
    receipt.state = "uncertain" if receipt.accepted and state != "accepted" else state
    receipt.message = (("Approval was accepted; " if receipt.accepted and state != "accepted" else "") + str(message))[:1000]
    receipt.accepted = receipt.accepted or state == "accepted"


def bind_origin(orch, request, chat):
    receipt = _CURRENT.get()
    if (receipt is not None and receipt.orchestrator is orch and receipt.request == str(request)
            and not receipt.accepted and isinstance(chat, str) and chat):
        receipt.chat = chat


def drop_connection(orch, websocket):
    cache = getattr(orch, "_approval_decision_receipts", {})
    for key, receipt in list(cache.items()):
        if receipt.websocket is websocket:
            cache.pop(key, None)


async def _send(receipt):
    if not receipt.current():
        return
    await receipt.orchestrator._safe_send(receipt.websocket, json.dumps({
        "type": "chat_status", "status": "approval_decision", "chat_id": receipt.chat,
        "connection_generation": str(receipt.generation),
        "approval_decision": {"request_id": receipt.request, "decision": receipt.decision,
                              "state": receipt.state, "message": receipt.message, "revision": 1}}))


async def run(orch, websocket, owner, payload, request_key, handler):
    claims = getattr(orch, "ui_sessions", {}).get(websocket) or {}
    if claims.get("_project_workspaces_v1") is not True:
        return await handler()
    connection = getattr(orch, "_connection_contexts", {}).get(id(websocket))
    request = payload.get(request_key) if isinstance(payload, dict) else None
    decision = payload.get("decision") if isinstance(payload, dict) else None
    chat = getattr(orch, "_ws_active_chat", {}).get(id(websocket))
    if (claims.get("sub") != owner or connection is None or not connection.registered or connection.closing
            or not isinstance(request, str) or re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", request) is None
            or decision not in {"approve", "approve_and_resume", "grant_task", "decline"}
            or not isinstance(chat, str) or not chat):
        return await handler()
    cache = getattr(orch, "_approval_decision_receipts", None)
    if cache is None:
        cache = orch._approval_decision_receipts = {}
    now = time.monotonic()
    for key, value in list(cache.items()):
        if not value.current() or value.future.done() and value.expires <= now:
            cache.pop(key, None)
    key = (owner, id(connection), request)
    existing = cache.get(key)
    if existing is not None:
        await asyncio.shield(existing.future)
        await _send(existing)
        return
    receipt = Receipt(orch, websocket, connection, connection.connection_generation, owner, chat, request,
                      decision, asyncio.get_running_loop().create_future(), now + TTL)
    if len(cache) >= MAXIMUM:
        receipt.state = "error"
        receipt.message = "Too many pending decisions. Wait for an existing decision to finish."
        await _send(receipt)
        return
    cache[key] = receipt
    token = _CURRENT.set(receipt)
    try:
        return await handler()
    except asyncio.CancelledError:
        receipt.state = "uncertain" if receipt.accepted else "cancelled"
        receipt.message = ("The approved action was interrupted; inspect its outcome before retrying."
                           if receipt.accepted else "The decision was cancelled before acceptance.")
        raise
    except Exception:
        receipt.state = "uncertain" if receipt.accepted else "error"
        receipt.message = ("The approved action did not finish reporting its outcome. Do not replay it."
                           if receipt.accepted else "The decision could not be completed. Reissue the action.")
        raise
    finally:
        _CURRENT.reset(token)
        if not receipt.future.done():
            receipt.future.set_result(None)
        await _send(receipt)
