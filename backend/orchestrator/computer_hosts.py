"""Owner-scoped registry of a user's remote-controllable desktop clients, correlating
computer_request/computer_response frames and pushing presence; backs the My
Computers surface and computer_sessions.py. Listings show one row per platform and
name, so an offline registration replaced by a newer one of the same computer is hidden.
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
from orchestrator.computer_use_policy import OBSERVE_VERBS

logger = logging.getLogger("ComputerHosts")

MAX_FRAME_BYTES = int(os.getenv("COMPUTER_HOST_MAX_FRAME_BYTES", str(4 * 1024 * 1024)))
WITHDRAWN = "consent_revoked"
WITHDRAWN_NOTE = "access withdrawn on that Mac; re-enable it in Computer controls there"
LAST_SEEN_TTL_S = int(os.getenv("COMPUTER_HOST_LAST_SEEN_TTL_S", str(24 * 3600)))


class ComputerHostError(Exception):
    def __init__(self, code: str, message: str, *, candidates: Optional[List[str]] = None, effect_state: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates or []
        self.effect_state = "not_started" if effect_state == "not_started" else None


@dataclass
class ComputerHost:
    owner_sub: str
    host_id: str
    websocket: Any
    descriptor: Dict[str, Any]
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    offline_reason: Optional[str] = None

    def unavailable(self) -> "ComputerHostError":
        if self.offline_reason == WITHDRAWN:
            return ComputerHostError("computer_unavailable", f"{self.name} withdrew computer access; ask the user "
                                                             "to re-enable it in Computer controls on that Mac")
        return ComputerHostError("computer_unavailable", f"{self.name} is offline — open the AstralDeep desktop "
                                                         "client on it")

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

    @property
    def workspace_roots(self) -> List[Dict[str, str]]:
        return [dict(root) for root in self.descriptor.get("workspace_roots") or []]

    def public(self, online: bool = True) -> Dict[str, Any]:
        return {
            "host_id": self.host_id,
            "name": self.name,
            "platform": self.platform,
            "client_version": str(self.descriptor.get("client_version") or ""),
            "screens": self.screens,
            "verbs": list(self.verbs),
            "workspace_roots": self.workspace_roots,
            "online": online,
            "offline_reason": None if online else self.offline_reason,
            "last_seen": int(self.last_seen),
        }


def _identity(row: Dict[str, Any]) -> Tuple[str, str]:
    return row["platform"], " ".join(row["name"].split()).casefold()


class ComputerHostRegistry:
    def __init__(self, orch):
        self._orch = orch
        self._hosts: Dict[Tuple[str, str], ComputerHost] = {}
        self._by_ws: Dict[int, Tuple[str, str]] = {}
        self._offline: Dict[Tuple[str, str], ComputerHost] = {}
        self._pending: Dict[str, Tuple[asyncio.Future, str, str, str, str]] = {}
        self._dropped_frames = 0

    def register(self, owner_sub: str, websocket, descriptor: ComputerHostDescriptor | Dict[str, Any]
                 ) -> Tuple[ComputerHost, Optional[ComputerHost]]:
        desc = descriptor.to_dict() if isinstance(descriptor, ComputerHostDescriptor) else dict(descriptor)
        key = (owner_sub, str(desc["host_id"]))
        superseded = None
        previous = self._hosts.get(key)
        if previous is not None:
            superseded = previous
            self._by_ws.pop(id(previous.websocket), None)
            self._fail_pending(key)
        stale_key = self._by_ws.get(id(websocket))
        if stale_key is not None and stale_key != key:
            superseded = self._hosts.pop(stale_key, None) or superseded
            self._fail_pending(stale_key)
        host = ComputerHost(owner_sub=owner_sub, host_id=key[1], websocket=websocket, descriptor=desc)
        self._hosts[key] = host
        self._by_ws[id(websocket)] = key
        self._offline.pop(key, None)
        logger.info("076: computer host online owner=%s host=%s name=%r verbs=%d",
                    owner_sub, key[1][:8], host.name, len(host.verbs))
        return host, superseded

    def _fail_pending(self, key) -> None:
        for fut, owner, host_id, verb, _session in list(self._pending.values()):
            if (owner, host_id) == key and not fut.done():
                fut.set_exception(ComputerHostError(
                    "host_offline" if verb in OBSERVE_VERBS else "outcome_uncertain",
                    "Host authority changed after delivery; inspect current state before retrying"))

    def cancel_session_requests(self, owner_sub: str, host_id: str, session_id: str) -> int:
        cancelled = 0
        for fut, owner, host, verb, session in list(self._pending.values()):
            if (owner, host, session) == (owner_sub, host_id, session_id) and not fut.done():
                fut.set_exception(ComputerHostError(
                    "cancelled" if verb in OBSERVE_VERBS else "outcome_uncertain",
                    f"The turn was cancelled while waiting for {verb}; inspect current state before retrying"))
                cancelled += 1
        return cancelled

    def withdraw(self, owner_sub: str, host_id: str) -> Optional[ComputerHost]:
        key = (owner_sub, host_id)
        host = self._hosts.pop(key, None)
        if host is not None:
            self._by_ws.pop(id(host.websocket), None)
            self._fail_pending(key)
            host.last_seen = time.time()
            host.offline_reason = WITHDRAWN
            self._offline[key] = host
        return host

    def forget(self, owner_sub: str, host_id: str) -> bool:
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
        self._fail_pending(key)
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
        offline: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for (o, _), h in list(self._offline.items()):
            if o != owner_sub:
                continue
            if now - h.last_seen > LAST_SEEN_TTL_S:
                self._offline.pop((o, h.host_id), None)
                continue
            row = h.public(False)
            identity = _identity(row)
            if identity not in offline or row["last_seen"] > offline[identity]["last_seen"]:
                offline[identity] = row
        online = {_identity(row) for row in out}
        out.extend(row for identity, row in offline.items() if identity not in online)
        out.sort(key=lambda d: (not d["online"], d["name"].lower()))
        return out

    def resolve(self, owner_sub: str, ref: Optional[str]) -> ComputerHost:
        online = self.online_for_owner(owner_sub)
        if ref is None or str(ref).strip() == "":
            if len(online) == 1:
                return online[0]
            if not online:
                known = [h for (o, _), h in self._offline.items() if o == owner_sub]
                if len({_identity(h.public(False)) for h in known}) == 1:
                    raise max(known, key=lambda h: h.last_seen).unavailable()
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
            raise known_offline[0].unavailable()
        raise ComputerHostError(
            "computer_unavailable", f"no computer called {wanted!r} is online",
            candidates=[h.name for h in online])

    async def request(self, host: ComputerHost, session_id: str, verb: str,
                      args: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        if self._offline.get((host.owner_sub, host.host_id)) is host and host.offline_reason == WITHDRAWN:
            raise host.unavailable()
        if (self._hosts.get((host.owner_sub, host.host_id)) is not host
                or (self._orch.ui_sessions.get(host.websocket) or {}).get("sub") != host.owner_sub):
            raise ComputerHostError("host_offline", "The selected host connection is no longer current")
        if verb not in COMPUTER_HOST_VERBS:
            raise ComputerHostError("unsupported", f"{verb!r} is not a host verb")
        if verb not in host.verbs:
            raise ComputerHostError("unsupported", f"{host.name} does not support {verb}")
        loop = asyncio.get_running_loop()
        request_id = f"creq_{uuid.uuid4().hex}"
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = (fut, host.owner_sub, host.host_id, verb, session_id)
        frame = json.dumps({
            "type": "computer_request",
            "request_id": request_id,
            "session_id": session_id,
            "verb": verb,
            "args": args,
            "deadline_ms": int(timeout * 1000),
        })
        try:
            async with asyncio.timeout(timeout):
                try:
                    sent = await self._orch._safe_send(host.websocket, frame)
                except Exception:
                    sent = False
                if sent is False:
                    raise ComputerHostError("host_offline" if verb in OBSERVE_VERBS else "outcome_uncertain",
                        f"Delivery to {host.name} could not be confirmed; inspect current state before retrying")
                payload = await fut
        except TimeoutError:
            code = "timeout" if verb in OBSERVE_VERBS else "outcome_uncertain"
            raise ComputerHostError(code, f"{host.name} did not answer {verb} within {timeout:g} s; inspect "
                                          "current state before retrying") from None
        finally:
            self._pending.pop(request_id, None)
        host.last_seen = time.time()
        if payload.get("ok") is True:
            result = payload.get("result")
            return result if isinstance(result, dict) else {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = str(error.get("code") or "failed")
        message = str(error.get("message") or f"{verb} failed on {host.name}")[:500]
        raise ComputerHostError(code, message, effect_state=error.get("effect_state"))

    def handle_response(self, websocket, owner_sub: str, payload: Dict[str, Any]) -> bool:
        request_id = str(payload.get("request_id") or "")
        entry = self._pending.get(request_id)
        if entry is None:
            return False
        fut, owner, host_id, _verb, _session = entry
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

    def over_size_cap(self, raw_len: int) -> bool:
        if raw_len > MAX_FRAME_BYTES:
            self._dropped_frames += 1
            return True
        return False

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

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
            except Exception:  # noqa: BLE001
                logger.debug("076: presence push failed", exc_info=True)
