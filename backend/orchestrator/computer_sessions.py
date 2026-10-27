"""Manages consent-gated remote-control sessions between an owner's client and a
computer host, with idle/duration caps, heartbeat watchdog, task grants from computer_grants.py,
the in-memory per-host bypass permissions switch, and audit logging; built on computer_hosts.py
and used by orchestrator.py and the agent tools.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import io
import json
import logging
import os
import posixpath
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from orchestrator.computer_grants import EVENT_CREATED, EVENT_EXPIRED, EVENT_REVOKED, EVENT_USED, GrantStore
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
STOP_REASONS = frozenset({"user_stop", "local_stop"})
LOCAL_RESUME_REFUSALS = {"local_pause": "local_pause_requires_local_resume", "local_input": "local_resume_required"}
_GRANT_REVOCATIONS = {
    "user_stop": "stop", "local_stop": "stop", "host_superseded": "host_replaced", "host_offline": "socket_lost",
    "host_silent": "socket_lost", "host_unresponsive": "socket_lost", "consent_revoked": "authority_changed",
}
_KILL_REASONS = END_REASONS - {"local_stop", "host_offline", "host_superseded", "host_silent",
                               "host_unresponsive", "consent_revoked"}
_FRAME_GRANT_KINDS = {"app": "application", "window": "application", "command": "program", "origin": "origin"}
_PUSHED_REVOCATIONS = frozenset({"replaced", "chat_changed"})
JOB_KILL_TIMEOUT_S = 3.0
MAX_COMMAND_JOBS = 64
SCREENSHOT_CACHE_BYTES = 3 * 1024 * 1024
CROP_PIXELS = 512
_CROP_PIXEL_LIMIT = 40_000_000
_MARKER_RGB = (255, 32, 160)


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
    window_labels: Dict[int, str] = field(default_factory=dict)
    window_cache: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    screenshot_cache: Optional[Dict[str, Any]] = None
    command_jobs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    terminal_ok_until: float = 0.0
    authority_generation: int = 0
    approval_pause_authority: Any = field(default=None, repr=False, compare=False)
    project_origin: Any = field(default=None, repr=False, compare=False)
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    verbs_run: int = 0

    @property
    def active(self) -> bool:
        return self.state == ACTIVE

    @property
    def terminal_ok(self) -> bool:
        return self.state == ACTIVE and self.terminal_ok_until > time.time()

    def grant_terminal(self, seconds: float) -> None:
        self.terminal_ok_until = time.time() + float(seconds)

    def forget_observations(self) -> None:
        self.window_labels.clear()
        self.window_cache.clear()
        self.screenshot_cache = None

    def remember_windows(self, windows: Sequence[Dict[str, Any]]) -> None:
        self.window_cache = {w["hwnd"]: {"bundle_id": w.get("bundle_id") or w.get("process") or "",
                                         "title": w.get("title") or "", "rect": w.get("rect"),
                                         "focused": bool(w.get("focused"))}
                             for w in windows if isinstance(w.get("hwnd"), int) and 1 <= w["hwnd"] <= 2**53}
        self.window_labels = {hwnd: " · ".join(part for part in (entry["bundle_id"], entry["title"]) if part)
                              for hwnd, entry in self.window_cache.items()}

    def remember_screenshot(self, b64: str, media_type: str, width: int, height: int, screen_index: int) -> None:
        self.screenshot_cache = None
        if not isinstance(b64, str) or len(b64) > (SCREENSHOT_CACHE_BYTES * 4) // 3 + 4:
            return
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return
        if len(data) <= SCREENSHOT_CACHE_BYTES:
            self.screenshot_cache = {"data": data, "media_type": media_type, "width": width, "height": height,
                                     "screen_index": screen_index}

    def pointer_crop(self, points: Sequence[Tuple[int, int]]) -> Optional[str]:
        shot = self.screenshot_cache
        if shot is None or not points:
            return None
        try:
            from PIL import Image as PILImage, ImageDraw
        except ImportError:
            return None
        try:
            with PILImage.open(io.BytesIO(shot["data"])) as image:
                width, height = image.size
                x, y = points[0]
                if width * height > _CROP_PIXEL_LIMIT or not (0 <= x < width and 0 <= y < height):
                    return None
                left = min(max(0, x - CROP_PIXELS // 2), max(0, width - CROP_PIXELS))
                top = min(max(0, y - CROP_PIXELS // 2), max(0, height - CROP_PIXELS))
                crop = image.convert("RGB").crop((left, top, min(width, left + CROP_PIXELS),
                                                  min(height, top + CROP_PIXELS)))
            draw = ImageDraw.Draw(crop)
            for px, py in points:
                cx, cy = px - left, py - top
                draw.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), outline=_MARKER_RGB, width=3)
                draw.line((cx - 22, cy, cx + 22, cy), fill=_MARKER_RGB, width=2)
                draw.line((cx, cy - 22, cx, cy + 22), fill=_MARKER_RGB, width=2)
            out = io.BytesIO()
            crop.save(out, "JPEG", quality=80)
        except Exception:  # noqa: BLE001
            logger.info("102: pointer crop unavailable for session %s", self.session_id[:12], exc_info=True)
            return None
        return base64.b64encode(out.getvalue()).decode("ascii")

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
            **({"project_context": self.project_origin.context.to_dict()} if self.project_origin else {}),
        }

    def frame(self, grants: Sequence[Dict[str, Any]] = (), bypass: bool = False) -> str:
        return json.dumps({"type": "computer_session", **self.public(), "grants": list(grants),
                           "bypass": bool(bypass)})


class ComputerSessionManager:
    def __init__(self, orch, registry: ComputerHostRegistry):
        self._orch = orch
        self._registry = registry
        self._sessions: Dict[str, ComputerSession] = {}
        self._sweeper: Optional[asyncio.Task] = None
        self._audit_tasks: set = set()
        self._push_pending: set = set()
        self._bypass: Dict[Tuple[str, str], ComputerHost] = {}
        self.grants = GrantStore(events=self._grant_event)

    def bypass_on(self, owner_sub: str, host_id: str) -> bool:
        host = self._bypass.get((owner_sub, host_id))
        return host is not None and self._registry.get(owner_sub, host_id) is host

    async def set_bypass(self, host: ComputerHost, enabled: bool, reason: str = "owner") -> None:
        key = (host.owner_sub, host.host_id)
        was = self.bypass_on(*key)
        if enabled:
            self._bypass[key] = host
        else:
            self._bypass.pop(key, None)
        if was != bool(enabled):
            await self._bypass_changed(host.owner_sub, host.host_id, host.name, bool(enabled), reason)

    async def clear_bypass(self, owner_sub: str, host_id: str, reason: str) -> bool:
        host = self._bypass.pop((owner_sub, host_id), None)
        if host is None:
            return False
        await self._bypass_changed(owner_sub, host_id, host.name, False, reason)
        return True

    async def _bypass_changed(self, owner_sub: str, host_id: str, name: str, enabled: bool, reason: str) -> None:
        try:
            from datetime import datetime, timezone

            from audit.recorder import get_recorder
            from audit.schemas import AuditEventCreate
            recorder = get_recorder()
            if recorder is not None:
                await recorder.record(AuditEventCreate(
                    actor_user_id=owner_sub, auth_principal=owner_sub, event_class="agent_lifecycle",
                    action_type="computer_bypass.enabled" if enabled else "computer_bypass.disabled",
                    description=f"bypass permissions {'on' if enabled else 'off'} for {name} ({reason})"[:1024],
                    correlation_id=host_id, outcome="success",
                    inputs_meta={"host_id": host_id, "reason": reason}, started_at=datetime.now(timezone.utc)))
        except Exception:  # noqa: BLE001
            logger.warning("102: bypass audit failed for host %s", host_id[:8], exc_info=True)
        for session in [s for s in self._sessions.values() if (s.owner_sub, s.host_id) == (owner_sub, host_id)]:
            await self._push(session)

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

    def resolve_host(self, owner_sub: str, chat_id: Optional[str], ref: Any) -> ComputerHost:
        if ref is None or str(ref).strip() == "":
            session = self.live_for_chat(owner_sub, chat_id)
            if session is None:
                live = self.live_for_owner(owner_sub)
                session = live[0] if len(live) == 1 else None
            host = self._registry.get(owner_sub, session.host_id) if session is not None else None
            if host is not None:
                return host
        return self._registry.resolve(owner_sub, ref)

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
                    *, wait_for_ack: bool = True, caller_guard=None) -> ComputerSession:
        from orchestrator.project_authority import resolve_project_origin, origin_current, ProjectSessionWitness
        previous = self.live_for_host(owner_sub, host.host_id)
        witness = ProjectSessionWitness.capture(self._orch, previous) if previous and host.project_mode else None
        origin = await resolve_project_origin(self._orch, owner_sub, chat_id, host)
        existing = self.live_for_host(owner_sub, host.host_id)
        if origin is not None and (existing is not previous or witness is not None and not witness.current(self._orch)):
            raise ComputerHostError("stale_project_grant", "The session changed while resolving this project.",
                                    effect_state="not_started")
        if caller_guard is not None:
            caller_guard()
        if existing is not None and origin is not None:
            previous = existing.project_origin
            if (existing.controller_ws_id != id(websocket) or previous is None
                    or previous.chat != chat_id or previous.context.project_id != origin.context.project_id
                    or previous.entry != origin.entry or not origin_current(self._orch, previous)):
                if existing.controller_ws_id != id(websocket) and self._controller_socket_alive(existing):
                    raise ComputerHostError("controlled_by_other", "Stop the current controller before taking over.")
                await self.end(existing, "consent_revoked", cancel_turn=False)
                existing = None
                if self.live_for_host(owner_sub, host.host_id) is not None:
                    raise ComputerHostError("stale_project_grant", "A newer session superseded this rotation.",
                                            effect_state="not_started")
                if caller_guard is not None:
                    caller_guard()
                if not origin_current(self._orch, origin):
                    raise ComputerHostError("stale_project_grant", "Project authority changed before session start.")
        if existing is not None:
            if existing.controller_ws_id == id(websocket):
                if chat_id and existing.chat_id != chat_id:
                    existing.terminal_ok_until = 0.0
                    existing.authority_generation += 1
                    existing.forget_observations()
                    self.grants.revoke(existing.session_id, "chat_changed")
                existing.chat_id = chat_id or existing.chat_id
                self.touch(existing)
                return existing
            if self._controller_socket_alive(existing):
                raise ComputerHostError(
                    "controlled_by_other",
                    f"{host.name} is already being controlled from your {existing.controller_label}; "
                    "stop that session first")
            existing.controller_ws_id = id(websocket)
            existing.terminal_ok_until = 0.0
            existing.authority_generation += 1
            existing.forget_observations()
            self.grants.revoke(existing.session_id, "authority_changed")
            existing.controller_label = self._controller_label(websocket)
            existing.controller_device_id = self._device_id(websocket)
            existing.chat_id = chat_id or existing.chat_id
            self.touch(existing)
            await self._push(existing)
            await self._audit(existing, "computer_session.taken_over",
                              f"session taken over from {existing.controller_label}")
            return existing

        session = ComputerSession(
            # Must stay a bare UUID: doubles as audit correlation_id
            session_id=str(uuid.uuid4()),
            owner_sub=owner_sub,
            host_id=host.host_id,
            host_name=host.name,
            controller_ws_id=id(websocket),
            controller_device_id=self._device_id(websocket),
            controller_label=self._controller_label(websocket),
            chat_id=chat_id,
            project_origin=origin,
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
        try:
            if caller_guard is not None:
                caller_guard()
        except ComputerHostError:
            await self.end(session, "consent_revoked", cancel_turn=False)
            raise
        if origin is not None and (not origin_current(self._orch, origin) or session.state != ACTIVE
                                  or session.authority_generation != 0 or self.get(session.session_id) is not session):
            await self.end(session, "consent_revoked", cancel_turn=False)
            raise ComputerHostError("stale_project_grant", "Project authority changed before session acknowledgment.")
        return session

    async def pause(self, session: ComputerSession, reason: str, *, push: bool = True) -> None:
        if session.state == ENDED or session.state == PAUSED and session.pause_reason == reason:
            return
        session.approval_pause_authority = None
        session.state = PAUSED
        session.terminal_ok_until = 0.0
        session.authority_generation += 1
        session.pause_reason = reason
        session.last_activity_at = time.time()
        self.grants.revoke(session.session_id, "takeover" if reason == "local_input" else "pause")
        await self._audit(session, "computer_session.paused", f"paused ({reason})")
        if push:
            await self._push(session)

    async def resume(self, session: ComputerSession, *, push: bool = True, local: bool = False) -> None:
        if session.state != PAUSED:
            return
        refusal = None if local else LOCAL_RESUME_REFUSALS.get(session.pause_reason or "")
        if refusal is not None:
            await self._audit(session, "computer_session.resume_refused", f"resume refused ({refusal})",
                              outcome="failure", meta={"reason": refusal, "pause_reason": session.pause_reason})
            raise ComputerHostError(refusal, f"{session.host_name} was paused at the computer; only its own Resume "
                                             "control can continue the session")
        session.approval_pause_authority = None
        session.state = ACTIVE
        session.authority_generation += 1
        session.pause_reason = None
        session.last_activity_at = time.time()
        await self._audit(session, "computer_session.resumed", "resumed")
        if push:
            await self._push(session)

    async def end(self, session: ComputerSession, reason: str, *, cancel_turn: bool = True) -> None:
        if session.state == ENDED:
            return
        if reason not in END_REASONS:
            reason = "user_stop"
        session.approval_pause_authority = None
        session.state = ENDED
        session.terminal_ok_until = 0.0
        session.authority_generation += 1
        session.reason = reason
        session.last_activity_at = time.time()
        self._sessions.pop(session.session_id, None)
        self.grants.revoke(session.session_id, _GRANT_REVOCATIONS.get(reason, "session_ended"))
        self._registry.cancel_session_requests(session.owner_sub, session.host_id, session.session_id)
        jobs = [job for job, record in session.command_jobs.items() if record.get("state", "running") == "running"]
        session.command_jobs.clear()
        host = self._registry.get(session.owner_sub, session.host_id)
        if jobs and host is not None and reason in _KILL_REASONS:
            await self._signal_jobs(host, session, jobs, "KILL")
        self._forget_browser(session)
        await self._audit(session, "computer_session.ended", f"ended ({reason})",
                          outcome="success" if reason in STOP_REASONS else "failure")
        await self._push(session)
        logger.info("076: session %s ended (%s) host=%s verbs=%d",
                    session.session_id[:12], reason, session.host_name, session.verbs_run)
        cancel = getattr(self._orch, "cancel_computer_turn", None)
        if cancel_turn and reason in STOP_REASONS and callable(cancel):
            outcome = cancel(session)
            if inspect.isawaitable(outcome):
                await outcome

    def cancel_requests(self, session: ComputerSession) -> int:
        return self._registry.cancel_session_requests(session.owner_sub, session.host_id, session.session_id)

    async def interrupt_jobs(self, session: ComputerSession, signal: str = "INT") -> None:
        jobs = [job for job, record in session.command_jobs.items() if record.get("state", "running") == "running"]
        host = self._registry.get(session.owner_sub, session.host_id)
        if jobs and host is not None and session.state != ENDED:
            await self._signal_jobs(host, session, jobs, signal)

    async def _signal_jobs(self, host: ComputerHost, session: ComputerSession, jobs: List[str], signal: str) -> None:
        results = await asyncio.gather(*(self._registry.request(
            host, session.session_id, "interrupt_command", {"job_id": job, "signal": signal}, JOB_KILL_TIMEOUT_S)
            for job in jobs), return_exceptions=True)
        for job, result in zip(jobs, results):
            if isinstance(result, BaseException):
                logger.info("102: job %s on %s not confirmed signalled (%s): %s", job[:12], session.host_name,
                            signal, getattr(result, "code", type(result).__name__))

    @staticmethod
    def _forget_browser(session: ComputerSession) -> None:
        forget = getattr(sys.modules.get("agents.computer_use.browser_tools"), "forget", None)
        if callable(forget):
            try:
                forget(session.session_id)
            except Exception:  # noqa: BLE001
                logger.warning("102: browser page state for session %s was not released", session.session_id[:12],
                               exc_info=True)

    def touch(self, session: ComputerSession) -> None:
        session.last_activity_at = time.time()

    async def end_all_for_host(self, owner_sub: str, host_id: str, reason: str) -> None:
        for s in [s for s in self._sessions.values()
                  if s.owner_sub == owner_sub and s.host_id == host_id]:
            await self.end(s, reason)

    async def end_all(self, reason: str) -> None:
        for s in list(self._sessions.values()):
            await self.end(s, reason)

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
            local = reason if reason in LOCAL_RESUME_REFUSALS else "local_input"
            if session.state == PAUSED and session.pause_reason == local:
                await self._push(session)
            else:
                await self.pause(session, local)
        elif event == "resumed":
            await self.resume(session, local=True)
        elif event == "stopped":
            await self.end(session, "local_stop")
        elif event == "approval_activity":
            await self._audit(session, "computer_session.approval_activity",
                              "input on the controlled host acted on an approval card")

    def _device_id(self, websocket) -> str:
        try:
            claims = self._orch.ui_sessions.get(websocket) or {}
            return str(claims.get("_device_id") or "") or f"ws-{id(websocket)}"
        except Exception:  # noqa: BLE001
            return f"ws-{id(websocket)}"

    def grant_rows(self, session: ComputerSession) -> List[Dict[str, Any]]:
        now = time.time()
        rows = []
        for grant in self.grants.list(session.session_id):
            if grant.expired_reason(now) is not None:
                continue
            scope = grant.scope
            detail: Dict[str, Any] = {"kind": scope["kind"]}
            if scope["kind"] == "window":
                detail["hwnd"] = scope["hwnd"]
            if scope["kind"] == "command":
                detail["root"] = grant.command["root"]
            target = (grant.command["argv0"] if scope["kind"] == "command"
                      else scope["origin"] if scope["kind"] == "origin" else scope["bundle_id"])
            rows.append({"id": grant.grant_id, "kind": _FRAME_GRANT_KINDS[scope["kind"]], "target": target,
                         "expires_at": int(grant.expires_at), "scope": detail})
        return rows

    async def revoke_grant(self, owner_sub: str, session_id: str, host_id: str, chat_id: str,
                           grant_id: str) -> bool:
        session = self._sessions.get(session_id)
        grant = next((g for g in self.grants.list(session_id) if g.grant_id == grant_id), None)
        if (session is None or grant is None or session.state == ENDED
                or (session.owner_sub, session.host_id, session.chat_id) != (owner_sub, host_id, chat_id)
                or (grant.owner_id, grant.host_id, grant.chat_id) != (owner_sub, host_id, chat_id)
                or self.grants.revoke_grant(grant_id, "user_revoked") is None):
            return False
        await self._push(session)
        return True

    def _schedule_push(self, session_id: Any) -> None:
        session = self._sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None or session.state == ENDED or session_id in self._push_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._push_pending.add(session_id)
        task = loop.create_task(self._push_later(session))
        self._audit_tasks.add(task)
        task.add_done_callback(self._audit_tasks.discard)

    async def _push_later(self, session: ComputerSession) -> None:
        self._push_pending.discard(session.session_id)
        if self._sessions.get(session.session_id) is session and session.state != ENDED:
            await self._push(session)

    async def _push(self, session: ComputerSession) -> None:
        frame = session.frame(self.grant_rows(session) if session.state != ENDED else (),
                              self.bypass_on(session.owner_sub, session.host_id))
        for ws in list(self._orch.ui_clients):
            try:
                if self._orch._get_user_id(ws) != session.owner_sub:
                    continue
                await self._orch._safe_send(ws, frame)
            except Exception:  # noqa: BLE001
                logger.debug("076: computer_session push failed", exc_info=True)

    async def _audit(self, session: ComputerSession, action_type: str, description: str,
                     *, outcome: str = "success", meta: Optional[Dict[str, Any]] = None) -> None:
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
                             "verbs_run": session.verbs_run, **(meta or {})},
                started_at=datetime.now(timezone.utc),
            ))
        except Exception:  # noqa: BLE001
            logger.debug("076: session audit failed (%s)", action_type, exc_info=True)

    def _grant_event(self, name: str, payload: Dict[str, Any]) -> None:
        if name in {EVENT_CREATED, EVENT_USED, EVENT_EXPIRED} or (
                name == EVENT_REVOKED and payload.get("reason") in _PUSHED_REVOCATIONS):
            self._schedule_push(payload.get("session_id"))
        try:
            from datetime import datetime, timezone

            from audit.recorder import get_recorder
            from audit.schemas import AuditEventCreate
            recorder = get_recorder()
            if recorder is None:
                return
            scope, command = payload.get("scope") or {}, payload.get("command") or {}
            meta: Dict[str, Any] = {key: payload[key] for key in ("grant_id", "host_id", "task_id", "verb", "reason")
                                    if key in payload}
            meta["scope"] = scope.get("kind")
            meta.update({key: scope[key] for key in ("bundle_id", "hwnd", "origin") if key in scope})
            if command:
                meta["argv0"], meta["folder"] = command.get("argv0"), posixpath.basename(command.get("root") or "")
            target = payload.get("target") or {}
            if isinstance(target.get("cwd"), str):
                meta["folder"] = posixpath.basename(target["cwd"].rstrip("/"))
            event = AuditEventCreate(
                actor_user_id=payload["owner_id"], auth_principal=payload["owner_id"],
                event_class="agent_lifecycle", action_type=name,
                description=f"task grant {name.rsplit('.', 1)[-1]} ({meta['scope']})",
                conversation_id=payload.get("chat_id"), correlation_id=payload["session_id"],
                outcome="success",
                inputs_meta=meta, started_at=datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001
            logger.warning("102: grant audit record could not be built (%s)", name, exc_info=True)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            try:
                recorder.record_blocking(event)
            except Exception:  # noqa: BLE001
                logger.warning("102: grant audit failed (%s)", name, exc_info=True)
            return
        task = loop.create_task(self._record_grant(recorder, event, name))
        self._audit_tasks.add(task)
        task.add_done_callback(self._audit_tasks.discard)

    @staticmethod
    async def _record_grant(recorder, event, name: str) -> None:
        try:
            await recorder.record(event)
        except Exception:  # noqa: BLE001
            logger.warning("102: grant audit failed (%s)", name, exc_info=True)

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
