"""Feature 076 — registry of the user's controllable desktops ("computer hosts").

A desktop client whose owner switched on "Allow remote control" announces a
:class:`shared.protocol.ComputerHostDescriptor` on its authenticated UI socket
(``register_ui.computer_host``, or a later ``computer_event: announce``). This
module keeps the owner-scoped registry keyed ``(owner_sub, host_id)``, pushes
presence to the owner's other clients, and correlates every
``computer_request`` push with its ``computer_response`` reply
(``request_id → Future``, the same shape as ``Orchestrator.pending_requests``).

Trust posture (spec FR-020/FR-022): the owner is ALWAYS the socket's verified
session ``sub``; a response is accepted only from the socket the request went
to; frames are size- and rate-capped per owner; results are untrusted data.
Nothing here persists — a host exists while its client is connected, plus a
last-seen cache so the surface can say "offline, last seen …" (D7).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from shared.protocol import COMPUTER_HOST_VERBS, ComputerHostDescriptor

logger = logging.getLogger("ComputerHosts")

MAX_FRAME_BYTES = int(os.getenv("COMPUTER_HOST_MAX_FRAME_BYTES", str(4 * 1024 * 1024)))
#: Offline hosts stay listed this long after their last sighting.
LAST_SEEN_TTL_S = int(os.getenv("COMPUTER_HOST_LAST_SEEN_TTL_S", str(24 * 3600)))


class ComputerHostError(Exception):
    """A typed failure the agent turns into a typed result (never a raise
    into the chat). ``code`` is one of the result-vocabulary codes."""

    def __init__(self, code: str, message: str, *, candidates: Optional[List[str]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates or []


@dataclass
class ComputerHost:
    owner_sub: str
    host_id: str
    websocket: Any
    descriptor: Dict[str, Any]
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    @property
    def name(self) -> str:
        return str(self.descriptor.get("name") or self.host_id[:8])

    @property
    def platform(self) -> str:
        return str(self.descriptor.get("platform") or "unknown")

    @property
    def verbs(self) -> Tuple[str, ...]:
        return tuple(self.descriptor.get("verbs") or ())

    @property
    def screens(self) -> List[Dict[str, Any]]:
        return list(self.descriptor.get("screens") or [])

    def public(self, online: bool = True) -> Dict[str, Any]:
        return {
            "host_id": self.host_id,
            "name": self.name,
            "platform": self.platform,
            "client_version": str(self.descriptor.get("client_version") or ""),
            "screens": self.screens,
            "verbs": list(self.verbs),
            "online": online,
            "last_seen": int(self.last_seen),
        }


class ComputerHostRegistry:
    """Owner-scoped, in-process registry + request/response correlation."""

    def __init__(self, orch):
        self._orch = orch
        self._hosts: Dict[Tuple[str, str], ComputerHost] = {}
        self._by_ws: Dict[int, Tuple[str, str]] = {}
        self._offline: Dict[Tuple[str, str], ComputerHost] = {}
        self._pending: Dict[str, Tuple[asyncio.Future, str, str, int]] = {}
        self._dropped_frames = 0

    # ── registration / presence ───────────────────────────────────────────────

    def register(self, owner_sub: str, websocket, descriptor: ComputerHostDescriptor | Dict[str, Any]
                 ) -> Tuple[ComputerHost, Optional[ComputerHost]]:
        """Register (or refresh) a host. Returns ``(host, superseded)`` where
        ``superseded`` is the previous record when a *different* socket held the
        same ``(owner, host_id)`` — the caller ends that record's session."""
        desc = descriptor.to_dict() if isinstance(descriptor, ComputerHostDescriptor) else dict(descriptor)
        key = (owner_sub, str(desc["host_id"]))
        superseded = None
        previous = self._hosts.get(key)
        if previous is not None and previous.websocket is not websocket:
            superseded = previous
            self._by_ws.pop(id(previous.websocket), None)
        # One socket announces at most one host: drop a stale key for this socket.
        stale_key = self._by_ws.get(id(websocket))
        if stale_key is not None and stale_key != key:
            self._hosts.pop(stale_key, None)
        host = ComputerHost(owner_sub=owner_sub, host_id=key[1], websocket=websocket, descriptor=desc)
        self._hosts[key] = host
        self._by_ws[id(websocket)] = key
        self._offline.pop(key, None)
        logger.info("076: computer host online owner=%s host=%s name=%r verbs=%d",
                    owner_sub, key[1][:8], host.name, len(host.verbs))
        return host, superseded

    def withdraw(self, owner_sub: str, host_id: str) -> Optional[ComputerHost]:
        """Consent revoked while connected: forget the host entirely (it is not
        listed as offline — the owner chose to remove it)."""
        key = (owner_sub, host_id)
        host = self._hosts.pop(key, None)
        if host is not None:
            self._by_ws.pop(id(host.websocket), None)
        self._offline.pop(key, None)
        return host

    def forget(self, owner_sub: str, host_id: str) -> bool:
        """Owner action from the surface: drop the last-seen record of an
        offline host. An online host is not forgettable (withdraw it first)."""
        return self._offline.pop((owner_sub, host_id), None) is not None

    def on_socket_closed(self, websocket) -> Optional[ComputerHost]:
        key = self._by_ws.pop(id(websocket), None)
        if key is None:
            return None
        host = self._hosts.pop(key, None)
        if host is None:
            return None
        host.last_seen = time.time()
        self._offline[key] = host
        # Fail every request still waiting on this socket.
        for request_id, (fut, owner, host_id, _size) in list(self._pending.items()):
            if (owner, host_id) == key and not fut.done():
                fut.set_exception(ComputerHostError("host_offline", f"{host.name} went offline"))
        logger.info("076: computer host offline owner=%s host=%s", key[0], key[1][:8])
        return host

    def host_for_socket(self, websocket) -> Optional[ComputerHost]:
        key = self._by_ws.get(id(websocket))
        return self._hosts.get(key) if key else None

    def get(self, owner_sub: str, host_id: str) -> Optional[ComputerHost]:
        return self._hosts.get((owner_sub, host_id))

    def online_for_owner(self, owner_sub: str) -> List[ComputerHost]:
        return [h for (o, _), h in self._hosts.items() if o == owner_sub]

    def list_for_owner(self, owner_sub: str) -> List[Dict[str, Any]]:
        now = time.time()
        out = [h.public(True) for h in self.online_for_owner(owner_sub)]
        for (o, _), h in list(self._offline.items()):
            if o != owner_sub:
                continue
            if now - h.last_seen > LAST_SEEN_TTL_S:
                self._offline.pop((o, h.host_id), None)
                continue
            out.append(h.public(False))
        out.sort(key=lambda d: (not d["online"], d["name"].lower()))
        return out

    def resolve(self, owner_sub: str, ref: Optional[str]) -> ComputerHost:
        """Resolve a host id or (unique, case-insensitive) name; with no
        reference, the owner's single online host."""
        online = self.online_for_owner(owner_sub)
        if ref is None or str(ref).strip() == "":
            if len(online) == 1:
                return online[0]
            if not online:
                raise ComputerHostError(
                    "computer_unavailable",
                    "none of your computers is online — open the AstralDeep desktop client "
                    "on the PC and switch on Settings → Remote control")
            raise ComputerHostError(
                "ambiguous_computer", "several computers are online — say which one",
                candidates=[h.name for h in online])
        wanted = str(ref).strip()
        for h in online:
            if h.host_id == wanted:
                return h
        matches = [h for h in online if h.name.lower() == wanted.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ComputerHostError(
                "ambiguous_computer", f"{len(matches)} online computers are named {wanted!r}",
                candidates=[h.host_id for h in matches])
        known_offline = [h for (o, _), h in self._offline.items()
                         if o == owner_sub and (h.host_id == wanted or h.name.lower() == wanted.lower())]
        if known_offline:
            raise ComputerHostError(
                "computer_unavailable",
                f"{known_offline[0].name} is offline — open the AstralDeep desktop client on it")
        raise ComputerHostError(
            "computer_unavailable", f"no computer called {wanted!r} is online",
            candidates=[h.name for h in online])

    # ── request / response correlation ────────────────────────────────────────

    async def request(self, host: ComputerHost, session_id: str, verb: str,
                      args: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        """Push one ``computer_request`` and await its correlated response.
        Raises :class:`ComputerHostError` (typed) — never returns None."""
        if verb not in COMPUTER_HOST_VERBS:
            raise ComputerHostError("unsupported", f"{verb!r} is not a host verb")
        if verb not in host.verbs:
            raise ComputerHostError("unsupported", f"{host.name} does not support {verb}")
        loop = asyncio.get_running_loop()
        request_id = f"creq_{uuid.uuid4().hex}"
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = (fut, host.owner_sub, host.host_id, 0)
        frame = json.dumps({
            "type": "computer_request",
            "request_id": request_id,
            "session_id": session_id,
            "verb": verb,
            "args": args,
            "deadline_ms": int(timeout * 1000),
        })
        try:
            sent = await self._orch._safe_send(host.websocket, frame)
            if sent is False:
                raise ComputerHostError("host_offline", f"{host.name} is not reachable")
            try:
                payload = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                raise ComputerHostError("timeout", f"{host.name} did not answer {verb} within {int(timeout)} s")
        finally:
            self._pending.pop(request_id, None)
        host.last_seen = time.time()
        if payload.get("ok") is True:
            result = payload.get("result")
            return result if isinstance(result, dict) else {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = str(error.get("code") or "failed")
        message = str(error.get("message") or f"{verb} failed on {host.name}")[:500]
        raise ComputerHostError(code, message)

    def handle_response(self, websocket, owner_sub: str, payload: Dict[str, Any]) -> bool:
        """Resolve the future for a ``computer_response``. Accepted only from
        the socket that holds the host the request went to (a phone, or a
        second desktop, can never answer for a host). Returns True if consumed."""
        request_id = str(payload.get("request_id") or "")
        entry = self._pending.get(request_id)
        if entry is None:
            return False
        fut, owner, host_id, _ = entry
        if owner != owner_sub:
            logger.warning("076: computer_response owner mismatch (dropped)")
            return False
        host = self._hosts.get((owner, host_id))
        if host is None or host.websocket is not websocket:
            logger.warning("076: computer_response from a non-host socket (dropped)")
            return False
        if fut.done():
            return False
        fut.set_result(payload)
        return True

    # ── caps ──────────────────────────────────────────────────────────────────

    def over_size_cap(self, raw_len: int) -> bool:
        if raw_len > MAX_FRAME_BYTES:
            self._dropped_frames += 1
            return True
        return False

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    # ── presence pushes ───────────────────────────────────────────────────────

    async def push_presence(self, host: ComputerHost, state: str) -> None:
        frame = json.dumps({
            "type": "computer_host",
            "host_id": host.host_id,
            "name": host.name,
            "platform": host.platform,
            "state": state,
        })
        for ws in list(self._orch.ui_clients):
            if ws is host.websocket:
                continue
            try:
                if self._orch._get_user_id(ws) != host.owner_sub:
                    continue
                await self._orch._safe_send(ws, frame)
            except Exception:  # noqa: BLE001 — best-effort presence
                logger.debug("076: presence push failed", exc_info=True)
