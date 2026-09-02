"""Feature 076 — remote-control sessions between an owner's client and one of
their computer hosts.

A session is the consent envelope every host verb runs inside (spec FR-005 to
FR-009): created only by an interactive, signed-in owner action; bound to
``(owner, host, controller socket, chat)``; ``active | paused | ended(reason)``;
idle- and hard-capped; heartbeat-watched; every transition audited under
``agent_lifecycle`` (``computer_session.*``) and pushed to every socket of the
owner as a ``computer_session`` frame (the host shows/hides its banner from it,
phones refresh the surface). Sessions live in memory (D7) — the audit trail is
the durable record.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from orchestrator.computer_hosts import ComputerHost, ComputerHostError, ComputerHostRegistry

logger = logging.getLogger("ComputerSessions")

IDLE_TIMEOUT_S = int(os.getenv("COMPUTER_USE_IDLE_TIMEOUT_S", str(20 * 60)))
MAX_DURATION_S = int(os.getenv("COMPUTER_USE_MAX_DURATION_S", str(2 * 3600)))
HEARTBEAT_SILENCE_S = int(os.getenv("COMPUTER_USE_HEARTBEAT_SILENCE_S", "90"))
ACK_TIMEOUT_S = float(os.getenv("COMPUTER_USE_ACK_TIMEOUT_S", "5"))
SWEEP_INTERVAL_S = 15.0

ACTIVE, PAUSED, ENDED = "active", "paused", "ended"
END_REASONS = frozenset({
    "user_stop", "local_stop", "consent_revoked", "host_offline", "host_superseded",
    "host_silent", "host_unresponsive", "idle_timeout", "max_duration",
    "controller_offline", "flag_off",
})


@dataclass
class ComputerSession:
    session_id: str
    owner_sub: str
    host_id: str
    host_name: str
    controller_ws_id: int
    controller_device_id: str
    controller_label: str
    chat_id: Optional[str]
    state: str = ACTIVE
    reason: Optional[str] = None
    pause_reason: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    images_supported: bool = True
    last_screenshot: Optional[Dict[str, Any]] = None
    #: Keystrokes into a terminal are allowed until this time (set by an
    #: approved confirm_action / shell open_app; 0 = not granted).
    terminal_ok_until: float = 0.0
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    verbs_run: int = 0

    @property
    def active(self) -> bool:
        return self.state == ACTIVE

    @property
    def terminal_ok(self) -> bool:
        return self.terminal_ok_until > time.time()

    def grant_terminal(self, seconds: float) -> None:
        self.terminal_ok_until = time.time() + float(seconds)

    def public(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "host_id": self.host_id,
            "host_name": self.host_name,
            "state": self.state,
            "reason": self.reason,
            "pause_reason": self.pause_reason,
            "controller_device_id": self.controller_device_id,
            "controller_label": self.controller_label,
            "chat_id": self.chat_id,
            "started_at": int(self.created_at),
            "last_activity_at": int(self.last_activity_at),
            "images_supported": self.images_supported,
            "verbs_run": self.verbs_run,
        }

    def frame(self) -> str:
        return json.dumps({"type": "computer_session", **self.public()})


class ComputerSessionManager:
    def __init__(self, orch, registry: ComputerHostRegistry):
        self._orch = orch
        self._registry = registry
        self._sessions: Dict[str, ComputerSession] = {}
        self._sweeper: Optional[asyncio.Task] = None

    # ── lookup ────────────────────────────────────────────────────────────────

    def get(self, session_id: str) -> Optional[ComputerSession]:
        return self._sessions.get(session_id)

    def live_for_host(self, owner_sub: str, host_id: str) -> Optional[ComputerSession]:
        for s in self._sessions.values():
            if s.owner_sub == owner_sub and s.host_id == host_id and s.state != ENDED:
                return s
        return None

    def live_for_owner(self, owner_sub: str) -> List[ComputerSession]:
        return [s for s in self._sessions.values() if s.owner_sub == owner_sub and s.state != ENDED]

    def live_for_chat(self, owner_sub: str, chat_id: Optional[str]) -> Optional[ComputerSession]:
        if not chat_id:
            return None
        for s in self.live_for_owner(owner_sub):
            if s.chat_id == chat_id:
                return s
        return None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _controller_socket_alive(self, session: ComputerSession) -> bool:
        return any(id(ws) == session.controller_ws_id for ws in self._orch.ui_clients)

    def _controller_label(self, websocket) -> str:
        try:
            profile = self._orch.rote.get_profile(websocket)
            dt = getattr(profile.device_type, "value", str(profile.device_type))
            return {"android": "Android phone", "ios": "iPhone", "macos": "Mac",
                    "windows": "Windows client", "browser": "web browser",
                    "tablet": "tablet", "mobile": "phone", "watch": "watch",
                    "tv": "TV", "voice": "voice client"}.get(dt, dt or "another device")
        except Exception:  # noqa: BLE001
            return "another device"

    async def start(self, owner_sub: str, host: ComputerHost, websocket, chat_id: Optional[str],
                    *, wait_for_ack: bool = True) -> ComputerSession:
        """Start (or re-join) a session on ``host`` for the controller socket.
        Raises :class:`ComputerHostError` with ``controlled_by_other`` when a
        different, still-connected controller holds the session."""
        existing = self.live_for_host(owner_sub, host.host_id)
        if existing is not None:
            if existing.controller_ws_id == id(websocket):
                existing.chat_id = chat_id or existing.chat_id
                self.touch(existing)
                return existing
            if self._controller_socket_alive(existing):
                raise ComputerHostError(
                    "controlled_by_other",
                    f"{host.name} is already being controlled from your {existing.controller_label}; "
                    "stop that session first")
            # Previous controller is gone — take over.
            existing.controller_ws_id = id(websocket)
            existing.controller_label = self._controller_label(websocket)
            existing.controller_device_id = self._device_id(websocket)
            existing.chat_id = chat_id or existing.chat_id
            self.touch(existing)
            await self._push(existing)
            await self._audit(existing, "computer_session.taken_over",
                              f"session taken over from {existing.controller_label}")
            return existing

        session = ComputerSession(
            # A bare UUID4: the audit table's correlation_id is a UUID column,
            # so the session id doubles as the audit correlation id verbatim.
            session_id=str(uuid.uuid4()),
            owner_sub=owner_sub,
            host_id=host.host_id,
            host_name=host.name,
            controller_ws_id=id(websocket),
            controller_device_id=self._device_id(websocket),
            controller_label=self._controller_label(websocket),
            chat_id=chat_id,
        )
        self._sessions[session.session_id] = session
        self._ensure_sweeper()
        await self._audit(session, "computer_session.started",
                          f"remote-control session started on {host.name} from {session.controller_label}")
        await self._push(session)
        if wait_for_ack:
            try:
                await asyncio.wait_for(session.acked.wait(), timeout=ACK_TIMEOUT_S)
            except asyncio.TimeoutError:
                await self.end(session, "host_unresponsive")
                raise ComputerHostError(
                    "host_unresponsive",
                    f"{host.name} did not acknowledge the session — its client may be an older "
                    "version without remote control")
        return session

    async def pause(self, session: ComputerSession, reason: str, *, push: bool = True) -> None:
        if session.state != ACTIVE:
            return
        session.state = PAUSED
        session.pause_reason = reason
        session.last_activity_at = time.time()
        await self._audit(session, "computer_session.paused", f"paused ({reason})")
        if push:
            await self._push(session)

    async def resume(self, session: ComputerSession, *, push: bool = True) -> None:
        if session.state != PAUSED:
            return
        session.state = ACTIVE
        session.pause_reason = None
        session.last_activity_at = time.time()
        await self._audit(session, "computer_session.resumed", "resumed")
        if push:
            await self._push(session)

    async def end(self, session: ComputerSession, reason: str) -> None:
        if session.state == ENDED:
            return
        if reason not in END_REASONS:
            reason = "user_stop"
        session.state = ENDED
        session.reason = reason
        session.last_activity_at = time.time()
        self._sessions.pop(session.session_id, None)
        await self._audit(session, "computer_session.ended", f"ended ({reason})",
                          outcome="success" if reason in ("user_stop", "local_stop") else "failure")
        await self._push(session)
        logger.info("076: session %s ended (%s) host=%s verbs=%d",
                    session.session_id[:12], reason, session.host_name, session.verbs_run)

    def touch(self, session: ComputerSession) -> None:
        session.last_activity_at = time.time()

    async def end_all_for_host(self, owner_sub: str, host_id: str, reason: str) -> None:
        for s in [s for s in self._sessions.values()
                  if s.owner_sub == owner_sub and s.host_id == host_id]:
            await self.end(s, reason)

    async def end_all(self, reason: str) -> None:
        for s in list(self._sessions.values()):
            await self.end(s, reason)

    # ── host-side events ──────────────────────────────────────────────────────

    async def on_host_event(self, owner_sub: str, host_id: str, event: str,
                            session_id: Optional[str], reason: Optional[str]) -> None:
        session = self._sessions.get(session_id or "")
        if session is None or session.owner_sub != owner_sub or session.host_id != host_id:
            return
        if event == "heartbeat":
            session.last_heartbeat_at = time.time()
            if not session.acked.is_set():
                session.acked.set()
        elif event == "paused":
            await self.pause(session, reason or "local_input")
        elif event == "resumed":
            await self.resume(session)
        elif event == "stopped":
            await self.end(session, "local_stop")

    # ── frames + audit ────────────────────────────────────────────────────────

    def _device_id(self, websocket) -> str:
        try:
            claims = self._orch.ui_sessions.get(websocket) or {}
            return str(claims.get("_device_id") or "") or f"ws-{id(websocket)}"
        except Exception:  # noqa: BLE001
            return f"ws-{id(websocket)}"

    async def _push(self, session: ComputerSession) -> None:
        frame = session.frame()
        for ws in list(self._orch.ui_clients):
            try:
                if self._orch._get_user_id(ws) != session.owner_sub:
                    continue
                await self._orch._safe_send(ws, frame)
            except Exception:  # noqa: BLE001
                logger.debug("076: computer_session push failed", exc_info=True)

    async def _audit(self, session: ComputerSession, action_type: str, description: str,
                     *, outcome: str = "success") -> None:
        try:
            from datetime import datetime, timezone

            from audit.recorder import get_recorder
            from audit.schemas import AuditEventCreate
            rec = get_recorder()
            if rec is None:
                return
            await rec.record(AuditEventCreate(
                actor_user_id=session.owner_sub,
                auth_principal=session.owner_sub,
                event_class="agent_lifecycle",
                action_type=action_type,
                description=description[:1024],
                conversation_id=session.chat_id,
                correlation_id=session.session_id,
                outcome=outcome,
                inputs_meta={"host_id": session.host_id, "controller": session.controller_label,
                             "verbs_run": session.verbs_run},
                started_at=datetime.now(timezone.utc),
            ))
        except Exception:  # noqa: BLE001 — audit is best-effort here; verbs audit separately
            logger.debug("076: session audit failed (%s)", action_type, exc_info=True)

    # ── watchdog ──────────────────────────────────────────────────────────────

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            try:
                self._sweeper = asyncio.get_running_loop().create_task(self._sweep_loop())
            except RuntimeError:
                self._sweeper = None

    async def _sweep_loop(self) -> None:
        try:
            while self._sessions:
                await asyncio.sleep(SWEEP_INTERVAL_S)
                await self.sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("076: sweep loop failed", exc_info=True)

    async def sweep(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        for s in list(self._sessions.values()):
            if now - s.created_at > MAX_DURATION_S:
                await self.end(s, "max_duration")
            elif now - s.last_activity_at > IDLE_TIMEOUT_S:
                await self.end(s, "idle_timeout")
            elif s.acked.is_set() and now - s.last_heartbeat_at > HEARTBEAT_SILENCE_S:
                await self.end(s, "host_silent")
