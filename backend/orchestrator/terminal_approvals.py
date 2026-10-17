"""Retains bounded REST terminal handoffs while normal human approval executes them.
Durable single-use decisions remain in remote_confirmation; request authority and results stay process-local.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import re
import threading
import time
from uuid import uuid4

from orchestrator.terminal_security import TerminalError, assert_terminal_identity

PREFIX = "terminal-api:"
MAX_PENDING_PER_OWNER = 8
MAX_REQUESTS = 512
REQUEST_TTL = 300
_REQUEST = ContextVar("terminal_approval_request", default=None)
_EFFECT = ContextVar("terminal_approval_effect", default=None)
_LOCK = threading.RLock()
_SESSION_FIELDS = frozenset({"session_id", "cols", "rows", "created_at_monotonic",
                             "idle_seconds", "ring_start", "ring_end"})


def _store(orch):
    with _LOCK:
        store = getattr(orch, "_terminal_approval_requests", None)
        if store is None:
            store = {}
            orch._terminal_approval_requests = store
        now = time.monotonic()
        for key, value in list(store.items()):
            if value.deadline <= now:
                del store[key]
        return store


class PendingTerminalRequest:
    def __init__(self, orch, owner_id, tool_name, check):
        self.orch, self.owner_id, self.tool_name, self.check = orch, owner_id, tool_name, check
        self.request_id = uuid4().hex
        self.proposal_id = None
        self.deadline = time.monotonic() + REQUEST_TTL
        self.status = "pending"
        self.session = None
        self.error = None

    @property
    def conversation_id(self):
        return PREFIX + self.request_id

    def create_proposal(self, create):
        self.check()
        with _LOCK:
            store = _store(self.orch)
            if self.proposal_id is not None or len(store) >= MAX_REQUESTS or sum(
                value.owner_id == self.owner_id and value.status in {"pending", "running"}
                for value in store.values()
            ) >= MAX_PENDING_PER_OWNER:
                raise TerminalError("terminal_approval_capacity", 429)
            proposal_id, card = create()
            self.proposal_id = proposal_id
            store[proposal_id] = self
            return proposal_id, card

    def assert_current(self):
        if self.deadline <= time.monotonic():
            raise TerminalError("terminal_approval_expired", 410)
        self.check()

    def public(self, owner_id):
        if owner_id != self.owner_id:
            raise TerminalError("terminal_approval_not_found", 404)
        self.assert_current()
        result = {"proposal_id": self.proposal_id, "status": self.status}
        if self.session is not None:
            result["session"] = deepcopy(self.session)
        if self.error is not None:
            result["error"] = self.error
        return result

    def complete(self, result=None, *, error=None):
        if error is None and result is not None and not getattr(result, "error", None):
            raw = getattr(result, "result", None)
            session = raw.get("session") if isinstance(raw, dict) else None
            if isinstance(session, dict) and isinstance(session.get("session_id"), str):
                clean = {key: value for key, value in session.items() if key in _SESSION_FIELDS}
                if all(type(value) in {str, int, float} for value in clean.values()):
                    self.session, self.status = clean, "succeeded"
                    return
        self.status = "failed"
        self.error = "terminal_dispatch_unavailable" if error is not None else "terminal_dispatch_refused"


@contextmanager
def request_scope(request):
    token = _REQUEST.set(request)
    try:
        yield
    finally:
        _REQUEST.reset(token)


def current_request(orch, owner_id, tool_name):
    request = _REQUEST.get()
    if (type(request) is PendingTerminalRequest and request.orch is orch
            and request.owner_id == owner_id and request.tool_name == tool_name
            and request.proposal_id is None):
        request.assert_current()
        return request
    return None


def get_request(orch, owner_id, proposal_id):
    if not isinstance(proposal_id, str) or not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
        raise TerminalError("terminal_approval_not_found", 404)
    with _LOCK:
        request = _store(orch).get(proposal_id)
    if request is None or request.owner_id != owner_id:
        raise TerminalError("terminal_approval_not_found", 404)
    request.assert_current()
    return request


def for_decision(orch, websocket, owner_id, row):
    from orchestrator.remote_confirmation import _no_live_human

    if _no_live_human(orch, websocket):
        raise TerminalError("terminal_human_approval_required", 403)
    assert_terminal_identity(orch, websocket, owner_id)
    request = get_request(orch, owner_id, str(row.proposal_id))
    if (request.status != "pending" or row.agent_id != "terminal-shell-1" or row.tool_name != request.tool_name
            or row.conversation_id != request.conversation_id):
        raise TerminalError("terminal_approval_not_found", 404)
    return request


@contextmanager
def effect_scope(request):
    request.assert_current()
    token = _EFFECT.set(request)
    try:
        yield
    finally:
        _EFFECT.reset(token)


def current_effect_check(orch, owner_id, tool_name):
    request = _EFFECT.get()
    if (type(request) is PendingTerminalRequest and request.orch is orch
            and request.owner_id == owner_id and request.tool_name == tool_name):
        request.assert_current()
        return request.assert_current
    return None
