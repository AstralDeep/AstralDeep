"""Per-chat review posture (Ask, Auto-review, Bypass) with the auto-review circuit breaker:
mode resolution defers to a host's live bypass switch, breaker trips return the chat to Ask,
and single-use reviewer allow verdicts carry an approved action through the card seams.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from orchestrator.computer_use_policy import AUTO_REVIEW_MODES

CONSECUTIVE_DENY_LIMIT = 3
TOTAL_DENY_LIMIT = 20
VERDICT_TTL_S = 120.0
_MAX_MODES = 512
_MAX_BREAKERS = 512
_MAX_VERDICTS = 128
_MAX_BREAKER_REASON = 300


class ReviewModeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _modes(orch) -> dict:
    store = getattr(orch, "_chat_review_modes", None)
    if not isinstance(store, dict):
        store = {}
        orch._chat_review_modes = store
    return store


def _breakers(orch) -> dict:
    store = getattr(orch, "_review_breakers", None)
    if not isinstance(store, dict):
        store = {}
        orch._review_breakers = store
    return store


def _verdicts(orch) -> dict:
    store = getattr(orch, "_auto_review_verdicts", None)
    if not isinstance(store, dict):
        store = {}
        orch._auto_review_verdicts = store
    return store


def _bounded(store: dict, limit: int) -> dict:
    while len(store) > limit:
        store.pop(next(iter(store)))
    return store


def stored_mode(orch, owner: str, chat: Optional[str]) -> str:
    if not owner or not chat:
        return "ask"
    return _modes(orch).get((owner, chat), "ask")


def set_mode(orch, owner: str, chat: Optional[str], mode: str) -> None:
    if mode not in AUTO_REVIEW_MODES or mode == "bypass":
        raise ReviewModeError("invalid_mode", "Chat review mode must be ask or auto_review.")
    if not owner or not chat:
        raise ReviewModeError("chat_required", "A review mode needs an owner and a conversation.")
    store = _modes(orch)
    store[(owner, chat)] = mode
    orch._chat_review_modes = _bounded(store, _MAX_MODES)
    _breakers(orch).pop((owner, chat), None)


def breaker(orch, owner: str, chat: Optional[str]) -> Optional[dict]:
    if not owner or not chat:
        return None
    entry = _breakers(orch).get((owner, chat))
    if entry is None:
        return None
    return {"open": True, "reason": entry["reason"]}


def record_denial(orch, owner: str, chat: str) -> tuple[bool, int, int, str]:
    store = _breakers(orch)
    entry = dict(store.get((owner, chat)) or {})
    consecutive = entry.get("consecutive", 0) + 1
    total = entry.get("total", 0) + 1
    tripped = consecutive >= CONSECUTIVE_DENY_LIMIT or total >= TOTAL_DENY_LIMIT
    reason = ""
    if tripped:
        reason = (f"Auto-review denied {consecutive} actions in a row ({total} in this conversation)."
                  if consecutive >= CONSECUTIVE_DENY_LIMIT else
                  f"Auto-review denied {total} actions in this conversation.")
        entry.update({"open": True, "reason": reason[:_MAX_BREAKER_REASON]})
        modes = _modes(orch)
        modes[(owner, chat)] = "ask"
        orch._chat_review_modes = _bounded(modes, _MAX_MODES)
    entry.update({"consecutive": consecutive, "total": total})
    store[(owner, chat)] = entry
    orch._review_breakers = _bounded(store, _MAX_BREAKERS)
    return tripped, consecutive, total, reason


def record_allow(orch, owner: str, chat: str) -> None:
    entry = _breakers(orch).get((owner, chat))
    if entry is not None:
        entry["consecutive"] = 0


def resolve_mode(orch, owner: str, chat: Optional[str], host_id: Optional[str] = None) -> str:
    if owner and chat and host_id:
        sessions = getattr(orch, "computer_sessions", None)
        if sessions is not None and sessions.bypass_on(owner, host_id):
            return "bypass"
    return stored_mode(orch, owner, chat)


def record_verdict(orch, owner: str, chat: str, agent: str, tool: str,
                   decision: str, reason: Optional[str], event_id: str) -> None:
    store = _verdicts(orch)
    store[(owner, chat, agent, tool)] = {
        "decision": decision, "reason": reason, "event_id": event_id,
        "expires": time.monotonic() + VERDICT_TTL_S,
    }
    orch._auto_review_verdicts = _bounded(store, _MAX_VERDICTS)


def peek_verdict(orch, owner: str, chat: str, agent: str, tool: str) -> Optional[dict]:
    entry = _verdicts(orch).get((owner, chat, agent, tool))
    if entry is None or entry["expires"] <= time.monotonic():
        return None
    return entry


def take_verdict(orch, owner: str, chat: str, agent: str, tool: str) -> Optional[dict]:
    key = (owner, chat, agent, tool)
    entry = _verdicts(orch).get(key)
    if entry is None or entry["expires"] <= time.monotonic():
        _verdicts(orch).pop(key, None)
        return None
    _verdicts(orch).pop(key, None)
    return entry


def session_mode(orch, session: Any) -> dict:
    from orchestrator.computer_use_policy import AUTO_REVIEW_MODES

    mode = resolve_mode(orch, session.owner_sub, session.chat_id, session.host_id)
    state: dict[str, Any] = {"mode": mode, "supported": sorted(AUTO_REVIEW_MODES)}
    if mode == "ask":
        tripped = breaker(orch, session.owner_sub, session.chat_id)
        if tripped is not None:
            state["breaker"] = {"open": True, "reason": tripped["reason"]}
    return state
