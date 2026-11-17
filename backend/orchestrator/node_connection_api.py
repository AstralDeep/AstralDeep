"""The personal node's operator sign-in API: list the node's outside connections and sign one in from the node's own machine, behind a loopback client and the operator secret that node_mesh.py keeps in the node data dir.
Each route asks the kernel through node_mesh.py and node_connections.py for the node's credential, answers only states and flow handles, and is included from node_api.py, which Vessel's connections command reaches from inside the node's container.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import math
import re
import time
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from orchestrator import node_connections, node_mesh
from shared import node_identity

logger = logging.getLogger("Orchestrator.NodeConnectionApi")

OPERATOR_HEADER = "astral-node-operator"
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for")
NO_STORE = {"Cache-Control": "no-store"}
MAX_BODY = 16384
MAX_URL = 8192
MAX_FLOWS = 4
FLOW_TTL_SECONDS = 600.0
CALLS_PER_WINDOW = 10
WINDOW_SECONDS = 60.0
CONNECTION_ID = re.compile(r"[0-9a-f]{32}")
FLOW_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
CODE = re.compile(r"[a-z0-9_.]{1,64}")
REDIRECTS = ("loopback", "pasted", "device_code")
STATUS = {
    "connection.flow_unknown": 404,
    "connection.unknown": 404,
    "connection.not_found": 404,
    "connection.flow_expired": 410,
}
EXPIRED_CODES = frozenset({"oauth.device_expired", "connection.flow_expired"})
DENIED_CODES = frozenset({"oauth.device_denied"})

connections_router = APIRouter(prefix="/api/node/connections", tags=["Node"])


def _refuse(status: int, code: str, headers: Optional[Dict[str, str]] = None) -> HTTPException:
    return HTTPException(status, code, headers={**NO_STORE, **(headers or {})})


def require_loopback(request: Request) -> None:
    try:
        address = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address is None or not address.is_loopback or any(name in request.headers for name in FORWARDING_HEADERS):
        raise _refuse(403, "node.loopback_required")


def operator_gate(request: Request, response: Response) -> None:
    if not node_identity.is_personal():
        raise _refuse(404, "node_profile_inactive")
    require_loopback(request)
    presented = request.headers.getlist(OPERATOR_HEADER)
    if len(presented) != 1 or not presented[0]:
        raise _refuse(401, "node.operator_secret_required")
    if not node_mesh.OperatorSecret(node_identity.data_dir()).matches(presented[0]):
        raise _refuse(401, "node.operator_secret_required")
    response.headers["Cache-Control"] = "no-store"


class Window:
    def __init__(self, limit: int = CALLS_PER_WINDOW, seconds: float = WINDOW_SECONDS):
        self._limit = limit
        self._seconds = seconds
        self._calls: Deque[float] = deque()

    def take(self, now: float) -> int:
        while self._calls and now - self._calls[0] >= self._seconds:
            self._calls.popleft()
        if len(self._calls) >= self._limit:
            return max(1, math.ceil(self._seconds - (now - self._calls[0])))
        self._calls.append(now)
        return 0


@dataclass
class Flow:
    connection: str
    started: float
    interval: int = 0


class FlowBook:
    def __init__(self) -> None:
        self.flows: "OrderedDict[str, Flow]" = OrderedDict()
        self.begin_lock = asyncio.Lock()
        self.windows = {"calls": Window(), "poll": Window()}


_books: "weakref.WeakKeyDictionary[Any, FlowBook]" = weakref.WeakKeyDictionary()
_clock: Callable[[], float] = time.monotonic


def _book(mesh: node_mesh.NodeMesh) -> FlowBook:
    book = _books.get(mesh)
    if book is None:
        book = _books[mesh] = FlowBook()
    return book


def _live(book: FlowBook) -> None:
    now = _clock()
    for flow in [name for name, item in book.flows.items() if now - item.started >= FLOW_TTL_SECONDS]:
        del book.flows[flow]


def _throttle(kind: str):
    def check() -> None:
        wait = _book(_mesh()).windows[kind].take(_clock())
        if wait:
            raise _refuse(429, "node.rate_limited", {"Retry-After": str(wait)})

    return check


def _mesh() -> node_mesh.NodeMesh:
    mesh = node_mesh.current()
    if mesh is None or not mesh.started:
        raise _refuse(503, "mesh_unavailable")
    return mesh


def _failure(exc: node_mesh.NodeMeshError) -> HTTPException:
    code = exc.code if CODE.fullmatch(exc.code) else "node.sign_in_failed"
    return _refuse(STATUS.get(code) or (exc.status if 400 <= exc.status < 500 else 409), code)


async def _call(mesh: node_mesh.NodeMesh, function: Callable[..., Any], *arguments: Any) -> Any:
    try:
        return await mesh.request_call(function, *arguments)
    except node_mesh.NodeMeshError as exc:
        raise _failure(exc) from None


async def _body(request: Request) -> Dict[str, Any]:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise _refuse(413, "node.request_too_large")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise _refuse(413, "node.request_too_large")
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise _refuse(400, "node.request_invalid")
    return parsed


def _redirect(binding: Any, body: Dict[str, Any]) -> Any:
    kind = body.get("redirect")
    if kind == "loopback":
        port = body.get("port")
        if set(body) != {"redirect", "port"} or type(port) is not int or not 1 <= port <= 65535:
            raise _refuse(400, "node.request_invalid")
        return binding.SignInRedirect.LOOPBACK(port)
    if kind not in REDIRECTS or set(body) != {"redirect"}:
        raise _refuse(400, "node.request_invalid")
    return binding.SignInRedirect.PASTED() if kind == "pasted" else binding.SignInRedirect.DEVICE_CODE()


def _known(book: FlowBook, flow: str) -> Flow:
    _live(book)
    if not FLOW_ID.fullmatch(flow) or flow not in book.flows:
        raise _refuse(404, "connection.flow_unknown")
    return book.flows[flow]


def _row(info: Dict[str, Any]) -> Dict[str, Any]:
    unsupported = info["unsupported"]
    device = info["this_device"]
    return {
        "id": info["id"],
        "name": info["name"],
        "status": "unsupported" if unsupported else "ok",
        "state": device["state"],
        "reason": unsupported or device["reason"],
    }


def _legacy_keys(request: Request, mesh: node_mesh.NodeMesh) -> Optional[Callable[[], Any]]:
    credentials = getattr(getattr(request.app.state, "orchestrator", None), "credential_manager", None)
    owner = mesh.personal_owner_id()
    if credentials is None or owner is None:
        return None
    return node_connections.legacy_keys_of(credentials, owner)


@connections_router.get("", dependencies=[Depends(operator_gate), Depends(_throttle("calls"))])
async def list_connections(request: Request):
    mesh = _mesh()
    listed = await node_connections.status(mesh, legacy_keys=_legacy_keys(request, mesh))
    if not listed["available"]:
        raise _refuse(503, listed.get("code") if CODE.fullmatch(str(listed.get("code"))) else "mesh_unavailable")
    return {
        "connections": [_row(item) for item in listed["connections"]],
        "legacy_connectors": listed["legacy_connectors"]["count"],
    }


@connections_router.post("/{connection}/sign-in", dependencies=[Depends(operator_gate), Depends(_throttle("calls"))])
async def begin_sign_in(connection: str, request: Request):
    if not CONNECTION_ID.fullmatch(connection):
        raise _refuse(404, "connection.unknown")
    mesh = _mesh()
    redirect = _redirect(mesh.binding, await _body(request))
    book = _book(mesh)
    async with book.begin_lock:
        _live(book)
        while len(book.flows) >= MAX_FLOWS:
            oldest, _ = book.flows.popitem(last=False)
            await _call(mesh, mesh.kernel.cancel_connection_sign_in, oldest)
        started = await _call(mesh, mesh.kernel.begin_connection_sign_in, connection, redirect)
        book.flows[started.flow] = Flow(connection, _clock(), started.interval_s or 0)
    if started.user_code is not None:
        remaining = max(1, min(int(FLOW_TTL_SECONDS), (started.expires_ms - time.time_ns() // 1_000_000) // 1000))
        return {
            "flow": started.flow,
            "user_code": started.user_code,
            "verification_uri": started.verification_uri,
            "interval": started.interval_s,
            "expires_in": remaining,
        }
    return {"flow": started.flow, "authorization_url": started.authorization_url}


@connections_router.post("/sign-in/{flow}/complete", dependencies=[Depends(operator_gate), Depends(_throttle("calls"))])
async def complete_sign_in(flow: str, request: Request):
    body = await _body(request)
    url = body.get("redirect_url")
    if set(body) != {"redirect_url"} or not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise _refuse(400, "node.request_invalid")
    mesh = _mesh()
    book = _book(mesh)
    _known(book, flow)
    book.flows.pop(flow, None)
    try:
        state = await _call(mesh, mesh.kernel.complete_connection_sign_in, flow, url)
    except HTTPException:
        with contextlib.suppress(HTTPException):
            await _call(mesh, mesh.kernel.cancel_connection_sign_in, flow)
        raise
    return {"state": state.state, "reason": state.reason}


@connections_router.post("/sign-in/{flow}/poll", dependencies=[Depends(operator_gate), Depends(_throttle("poll"))])
async def poll_sign_in(flow: str):
    mesh = _mesh()
    book = _book(mesh)
    entry = _known(book, flow)
    try:
        polled = await mesh.request_call(mesh.kernel.poll_connection_sign_in, flow)
    except node_mesh.NodeMeshError as exc:
        if exc.code in EXPIRED_CODES | DENIED_CODES:
            book.flows.pop(flow, None)
            return {"status": "expired_token" if exc.code in EXPIRED_CODES else "access_denied"}
        if exc.code == "connection.flow_unknown":
            book.flows.pop(flow, None)
        raise _failure(exc) from None
    if polled.is_DONE():
        book.flows.pop(flow, None)
        if polled.state.state != "signed_in":
            raise _refuse(409, "connection.not_signed_in")
        return {"status": "signed_in"}
    slower = polled.interval_s > entry.interval
    entry.interval = polled.interval_s
    return {"status": "slow_down" if slower else "authorization_pending"}


@connections_router.delete("/sign-in/{flow}", dependencies=[Depends(operator_gate), Depends(_throttle("calls"))])
async def cancel_sign_in(flow: str):
    mesh = _mesh()
    book = _book(mesh)
    _known(book, flow)
    book.flows.pop(flow, None)
    await _call(mesh, mesh.kernel.cancel_connection_sign_in, flow)
    return {"cancelled": True}
