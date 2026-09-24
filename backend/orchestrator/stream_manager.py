"""Owns the lifecycle of every push-streaming subscription: state machine, retry
backoff, and coalesced per-subscriber delivery of agent-emitted chunks to websockets.
Instantiated by orchestrator.py; agent-side counterpart is shared/stream_sdk.py.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any, Awaitable, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING,
)

from orchestrator.workspace import fingerprint as workspace_fingerprint
from shared.feature_flags import flags

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import WebSocket

logger = logging.getLogger("StreamManager")


MAX_STREAM_SUBSCRIPTIONS = 10

MAX_DORMANT_PER_USER = 50

DORMANT_TTL_SECONDS = 3600

DEFAULT_MAX_CHUNK_BYTES = 65536

DEFAULT_MAX_FPS = 30
DEFAULT_MIN_FPS = 5

MAX_PARAMS_BYTES = 16384

SWEEP_INTERVAL_SECONDS = 60.0

RETRY_BACKOFF_SECONDS: Tuple[float, ...] = (1.0, 5.0, 15.0)

MAX_RETRY_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)


class StreamState(enum.Enum):
    STARTING = "starting"
    ACTIVE = "active"
    RECONNECTING = "reconnecting"
    DORMANT = "dormant"
    STOPPED = "stopped"
    FAILED = "failed"


StreamKey = Tuple[str, str, str, str]


@dataclass
class StreamChunk:
    stream_id: str
    seq: int
    components: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None
    terminal: bool = False


@dataclass
class StreamSubscription:
    stream_id: str
    user_id: str
    chat_id: str
    tool_name: str
    agent_id: str
    params: Dict[str, Any]
    params_hash: str
    component_id: str
    subscribers: List["WebSocket"] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    last_chunk_at: Optional[float] = None
    state: StreamState = StreamState.STARTING
    state_reason: Optional[str] = None

    retry_attempt: int = 0
    next_retry_at: Optional[float] = None
    last_error_code: Optional[str] = None
    _retry_handle: Optional[asyncio.TimerHandle] = field(default=None, repr=False)

    task: Optional[asyncio.Task] = field(default=None, repr=False)
    request_id: Optional[str] = None
    coalesce_slot: Optional[StreamChunk] = field(default=None, repr=False)
    send_in_progress: bool = False
    last_send_at: float = 0.0

    delivered_count: int = 0
    dropped_count: int = 0

    retained_chunk: Optional[StreamChunk] = field(default=None, repr=False)
    max_seq_seen: int = 0
    seq_offset: int = 0
    persist_done: bool = False

    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES
    max_fps: int = DEFAULT_MAX_FPS
    min_fps: int = DEFAULT_MIN_FPS

    @property
    def key(self) -> StreamKey:
        return (self.user_id, self.chat_id, self.tool_name, self.params_hash)

    @property
    def bridged_component_id(self) -> Optional[str]:
        return self.component_id if self.component_id != self.stream_id else None


def params_hash(params: Dict[str, Any]) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def compute_backoff(attempt: int) -> float:
    if not (1 <= attempt <= MAX_RETRY_ATTEMPTS):
        raise ValueError(f"retry attempt out of range: {attempt}")
    base = RETRY_BACKOFF_SECONDS[attempt - 1]
    return base * random.uniform(0.8, 1.2)


ErrorClass = str

_ERROR_CLASSIFICATION: Dict[str, ErrorClass] = {
    "tool_error": "transient",
    "upstream_unavailable": "transient",
    "rate_limited": "transient",
    "unauthenticated": "auth",
    "unauthorized": "auth",
    "chunk_too_large": "terminal",
    "cancelled": "terminal",
}


def classify_error(code: str) -> ErrorClass:
    return _ERROR_CLASSIFICATION.get(code, "transient")


_LIST_MARKER = re.compile(r"\s*(?:[-*]|\d+\.)[ \t]+")


def _inline_safe_len(line: str) -> int:
    marker = _LIST_MARKER.match(line)
    content_start = marker.end() if marker else 0
    bold = italic = code = False
    link_depth = 0
    in_target = False
    pending_target = False
    safe = 0
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if pending_target:
            pending_target = False
            if ch == "(":
                in_target = True
                i += 1
                continue
        if ch == "`":
            code = not code
            i += 1
            continue
        if code:
            i += 1
            continue
        if in_target:
            if ch == ")":
                in_target = False
            i += 1
            continue
        if ch == "*":
            run = 1
            while i + run < n and line[i + run] == "*":
                run += 1
            if i >= content_start:
                if (run // 2) % 2:
                    bold = not bold
                if run % 2:
                    italic = not italic
            i += run
            continue
        if ch == "[":
            link_depth += 1
        elif ch == "]" and link_depth:
            link_depth -= 1
            if not link_depth:
                pending_target = True
        elif ch.isspace() and i > content_start and not (bold or italic or link_depth):
            safe = i + 1
        i += 1
    return safe


def markdown_safe_prefix_len(text: str) -> int:
    safe = 0
    fence = False
    pos = 0
    lines = text.split("\n")
    for line in lines[:-1]:
        pos += len(line) + 1
        if line.strip().startswith("```"):
            fence = not fence
        if not fence:
            safe = pos
    tail = lines[-1]
    if not fence and not tail.lstrip().startswith("```"):
        tail_safe = _inline_safe_len(tail)
        if tail_safe:
            safe = pos + tail_safe
    return safe


SendFn = Callable[["WebSocket", Any], Awaitable[None]]

GetSessionFn = Callable[["WebSocket"], Optional[Dict[str, Any]]]

AgentDispatchFn = Callable[
    [
        str,
        str,
        Dict[str, Any],
        str,
        Optional[str],
        "WebSocket",
        str,
    ],
    Awaitable[str],
]

AgentCancelFn = Callable[[str, str, str], Awaitable[None]]


class StreamManager:
    def __init__(
        self,
        rote: Any,
        send_to_ws: SendFn,
        get_user_session: GetSessionFn,
        agent_dispatcher: Optional[AgentDispatchFn] = None,
        agent_canceller: Optional[AgentCancelFn] = None,
        validate_chat_ownership: Optional[Callable[[Any, str, str], bool]] = None,
    ) -> None:
        self._rote = rote
        self._send_to_ws = send_to_ws
        self._get_user_session = get_user_session
        self._agent_dispatcher = agent_dispatcher
        self._agent_canceller = agent_canceller
        self._validate_chat_ownership = validate_chat_ownership

        self._active: Dict[StreamKey, StreamSubscription] = {}
        self._dormant: Dict[Tuple[str, str], Dict[str, StreamSubscription]] = {}
        self._request_to_key: Dict[str, StreamKey] = {}

        self._sweep_task: Optional[asyncio.Task] = None
        self._shutdown = False

        self.terminal_hook: Optional[Callable[[StreamSubscription], Awaitable[None]]] = None

    async def _fire_terminal_hook(self, sub: StreamSubscription) -> None:
        if self.terminal_hook is None:
            return
        try:
            await self.terminal_hook(sub)
        except Exception:
            logger.exception(
                "stream_artifacts.terminal_hook_failed stream=%s component=%s "
                "agent=%s tool=%s chat=%s user=%s",
                sub.stream_id, sub.bridged_component_id, sub.agent_id,
                sub.tool_name, sub.chat_id, sub.user_id)

    async def subscribe(
        self,
        ws: "WebSocket",
        user_id: str,
        chat_id: str,
        tool_name: str,
        agent_id: str,
        params: Dict[str, Any],
        tool_metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, bool]:
        session = self._get_user_session(ws)
        if session is None:
            raise ValueError("websocket has no active session")
        if session.get("sub") != user_id:
            raise ValueError("user_id does not match the websocket's session")

        if self._validate_chat_ownership is not None:
            if not await self._validate_chat_ownership(ws, user_id, chat_id):
                raise ValueError(f"chat {chat_id!r} is not owned by this user")

        self._validate_params_size(params)

        ph = params_hash(params)
        key: StreamKey = (user_id, chat_id, tool_name, ph)
        if key not in self._active and self._count_active_for_user(user_id) >= MAX_STREAM_SUBSCRIPTIONS:
            raise ValueError(
                f"per-user stream limit ({MAX_STREAM_SUBSCRIPTIONS}) exceeded"
            )

        existing = self._active.get(key)
        if existing is not None:
            if ws not in existing.subscribers:
                existing.subscribers.append(ws)
                logger.info(
                    f"subscribe({existing.stream_id}): attached additional "
                    f"ws (subscribers={len(existing.subscribers)})"
                )
                await self.replay_retained(ws, existing.stream_id)
            return existing.stream_id, True

        dormant_chat = self._dormant.get((user_id, chat_id), {})
        dormant_existing = dormant_chat.get(ph)
        if dormant_existing is not None:
            dormant_chat.pop(ph, None)
            if not dormant_chat:
                self._dormant.pop((user_id, chat_id), None)
            dormant_existing.subscribers = [ws]
            dormant_existing.state = StreamState.STARTING
            dormant_existing.state_reason = "wake_on_attach"
            dormant_existing.retry_attempt = 0
            dormant_existing.next_retry_at = None
            dormant_existing.coalesce_slot = None
            dormant_existing.send_in_progress = False
            dormant_existing.delivered_count = 0
            self._active[key] = dormant_existing
            if self._agent_dispatcher is not None:
                try:
                    request_id = await self._agent_dispatcher(
                        dormant_existing.agent_id, dormant_existing.tool_name,
                        dormant_existing.params, dormant_existing.stream_id, user_id,
                        ws, dormant_existing.chat_id,
                    )
                    dormant_existing.request_id = request_id
                    self._request_to_key[request_id] = key
                except Exception as e:
                    self._active.pop(key, None)
                    raise ValueError(f"agent dispatch failed: {e}") from e
            return dormant_existing.stream_id, False

        stream_id = f"stream-{uuid.uuid4().hex[:12]}"
        max_chunk_bytes = (
            (tool_metadata or {}).get("max_chunk_bytes", DEFAULT_MAX_CHUNK_BYTES)
        )
        max_fps = (tool_metadata or {}).get("max_fps", DEFAULT_MAX_FPS)
        min_fps = (tool_metadata or {}).get("min_fps", DEFAULT_MIN_FPS)

        sub = StreamSubscription(
            stream_id=stream_id,
            user_id=user_id,
            chat_id=chat_id,
            tool_name=tool_name,
            agent_id=agent_id,
            params=params,
            params_hash=ph,
            component_id=(
                workspace_fingerprint(agent_id, tool_name, params)
                if flags.is_enabled("stream_artifacts") else stream_id
            ),
            subscribers=[ws],
            state=StreamState.STARTING,
            max_chunk_bytes=max_chunk_bytes,
            max_fps=max_fps,
            min_fps=min_fps,
        )
        self._active[key] = sub

        self._ensure_sweeper_running()

        if self._agent_dispatcher is None:
            logger.debug(
                f"subscribe({stream_id}): no agent_dispatcher, skipping dispatch"
            )
            return stream_id, False

        try:
            request_id = await self._agent_dispatcher(
                agent_id, tool_name, params, stream_id, user_id,
                ws, chat_id,
            )
        except Exception as e:
            self._active.pop(key, None)
            raise ValueError(f"agent dispatch failed: {e}") from e

        sub.request_id = request_id
        self._request_to_key[request_id] = key
        logger.info(
            f"subscribe({stream_id}): dispatched to agent {agent_id} as "
            f"request {request_id}"
        )
        return stream_id, False

    async def unsubscribe(self, ws: "WebSocket", stream_id: str) -> None:
        target_sub = None
        for sub in list(self._active.values()):
            if sub.stream_id == stream_id:
                target_sub = sub
                break
        if target_sub is None:
            raise ValueError(f"unknown stream_id {stream_id!r}")

        session = self._get_user_session(ws)
        if session is None or session.get("sub") != target_sub.user_id:
            raise ValueError("not authorized to unsubscribe this stream")

        if ws in target_sub.subscribers:
            target_sub.subscribers.remove(ws)

        if not target_sub.subscribers:
            await self._cancel_on_agent(target_sub)
            terminal = StreamChunk(
                stream_id=target_sub.stream_id,
                seq=target_sub.max_seq_seen + 1,
                components=[],
                terminal=True,
            )
            try:
                await self._send_chunk_to_ws(target_sub, ws, terminal)
            except Exception:
                pass
            self._teardown_subscription(target_sub, StreamState.STOPPED, reason="unsubscribe")
            await self._fire_terminal_hook(target_sub)

    async def detach(self, ws: "WebSocket") -> None:
        for sub in list(self._active.values()):
            if ws in sub.subscribers:
                sub.subscribers.remove(ws)
                if not sub.subscribers:
                    await self._move_to_dormant(sub, reason="ws_disconnect")

    async def pause_chat(self, ws: "WebSocket", old_chat_id: str) -> None:
        for sub in list(self._active.values()):
            if sub.chat_id != old_chat_id:
                continue
            if ws in sub.subscribers:
                sub.subscribers.remove(ws)
                if not sub.subscribers:
                    await self._move_to_dormant(sub, reason="load_chat_away")

    async def _move_to_dormant(self, sub: StreamSubscription, reason: str) -> None:
        await self._cancel_on_agent(sub)

        if sub._retry_handle is not None:
            sub._retry_handle.cancel()
            sub._retry_handle = None
        sub.retry_attempt = 0
        sub.next_retry_at = None
        sub.task = None
        sub.coalesce_slot = None
        sub.send_in_progress = False

        self._active.pop(sub.key, None)
        if sub.request_id:
            self._request_to_key.pop(sub.request_id, None)
            sub.request_id = None
        sub.state = StreamState.DORMANT
        sub.state_reason = reason

        if self._count_dormant_for_user(sub.user_id) >= MAX_DORMANT_PER_USER:
            self._evict_oldest_dormant_for_user(sub.user_id)

        chat_dict = self._dormant.setdefault((sub.user_id, sub.chat_id), {})
        chat_dict[sub.params_hash] = sub
        sub.created_at = time.monotonic()
        logger.info(
            f"subscription {sub.stream_id} → DORMANT "
            f"(reason={reason}, user={sub.user_id}, chat={sub.chat_id})"
        )

    async def _cancel_on_agent(self, sub: StreamSubscription) -> None:
        if self._agent_canceller is None or not sub.request_id:
            return
        try:
            await self._agent_canceller(sub.agent_id, sub.request_id, sub.stream_id)
        except Exception as e:
            logger.debug(
                f"_cancel_on_agent failed for {sub.stream_id}: {e}"
            )

    def _evict_oldest_dormant_for_user(self, user_id: str) -> None:
        oldest_sub = None
        oldest_outer_key = None
        oldest_inner_key = None
        for outer_key, entries in self._dormant.items():
            if outer_key[0] != user_id:
                continue
            for inner_key, s in entries.items():
                if oldest_sub is None or s.created_at < oldest_sub.created_at:
                    oldest_sub = s
                    oldest_outer_key = outer_key
                    oldest_inner_key = inner_key
        if oldest_sub is not None:
            self._dormant[oldest_outer_key].pop(oldest_inner_key, None)
            if not self._dormant[oldest_outer_key]:
                self._dormant.pop(oldest_outer_key, None)
            logger.info(
                f"dormant LRU evicted {oldest_sub.stream_id} for user {user_id}"
            )

    async def _send_chunk_to_ws(
        self, sub: StreamSubscription, ws: "WebSocket", chunk: StreamChunk,
        include_html: bool = False,
    ) -> None:
        adapted_components = chunk.components
        if chunk.components and self._rote is not None:
            try:
                adapted_components = self._rote.adapt(ws, chunk.components)
            except Exception:
                pass
        wire_msg = {
            "type": "ui_stream_data",
            "stream_id": sub.stream_id,
            "session_id": sub.chat_id,
            "seq": chunk.seq,
            "components": adapted_components,
            "raw": chunk.raw,
            "terminal": chunk.terminal,
            "error": chunk.error,
        }
        if include_html:
            chunk_html = None
            if adapted_components:
                try:
                    from webrender import render_for_target, target_for_profile
                    profile = self._rote.get_profile(ws) if self._rote is not None else None
                    chunk_html = render_for_target(
                        target_for_profile(profile), adapted_components, profile)
                except Exception:
                    logger.exception("webrender: failed to render stream chunk")
            wire_msg["html"] = chunk_html
        if sub.bridged_component_id is not None:
            wire_msg["component_id"] = sub.bridged_component_id
        await self._send_to_ws(ws, json.dumps(wire_msg))

    async def resume(self, ws: "WebSocket", user_id: str, chat_id: str) -> List[Tuple[str, str]]:
        outer_key = (user_id, chat_id)
        dormant_dict = self._dormant.get(outer_key)
        if not dormant_dict:
            return []

        session = self._get_user_session(ws)
        if session is None or session.get("sub") != user_id:
            logger.warning(
                f"resume called with mismatched user/session for {user_id}"
            )
            return []

        resumed: List[Tuple[str, str]] = []
        for params_hash_key, sub in list(dormant_dict.items()):
            try:
                dormant_dict.pop(params_hash_key, None)
                sub.subscribers = [ws]
                sub.state = StreamState.STARTING
                sub.state_reason = "resumed"
                sub.retry_attempt = 0
                sub.next_retry_at = None
                sub.last_error_code = None
                sub.coalesce_slot = None
                sub.send_in_progress = False
                sub.delivered_count = 0
                sub.dropped_count = 0
                sub.seq_offset = sub.max_seq_seen

                self._active[sub.key] = sub

                if self._agent_dispatcher is not None:
                    try:
                        request_id = await self._agent_dispatcher(
                            sub.agent_id, sub.tool_name, sub.params,
                            sub.stream_id, user_id,
                            ws, sub.chat_id,
                        )
                        sub.request_id = request_id
                        self._request_to_key[request_id] = sub.key
                    except Exception as e:
                        logger.warning(
                            f"resume dispatch failed for {sub.stream_id}: {e}"
                        )
                        err_chunk = StreamChunk(
                            stream_id=sub.stream_id,
                            seq=sub.max_seq_seen + 1,
                            components=[],
                            error={
                                "code": "upstream_unavailable",
                                "message": (
                                    f"Stream cannot be resumed: {e}. "
                                    f"Click retry to start a new subscription."
                                ),
                                "phase": "failed",
                                "retryable": True,
                            },
                            terminal=True,
                        )
                        try:
                            await self._send_chunk_to_ws(sub, ws, err_chunk)
                        except Exception:
                            pass
                        self._teardown_subscription(
                            sub, StreamState.FAILED, reason="resume_dispatch_failed",
                        )
                        await self._fire_terminal_hook(sub)
                        continue

                resumed.append((sub.stream_id, sub.tool_name))
                logger.info(
                    f"subscription {sub.stream_id} → STARTING (resumed for "
                    f"user={user_id}, chat={chat_id})"
                )
            except Exception as e:  # pragma: no cover
                logger.error(f"resume of {sub.stream_id} raised: {e}")

        if not dormant_dict:
            self._dormant.pop(outer_key, None)

        return resumed

    async def attach_to_chat(
        self, ws: "WebSocket", user_id: str, chat_id: str,
    ) -> List[Tuple[str, str]]:
        session = self._get_user_session(ws)
        if session is None or session.get("sub") != user_id:
            logger.warning(
                f"attach_to_chat called with mismatched user/session for {user_id}"
            )
            return []
        attached: List[Tuple[str, str]] = []
        for sub in self._active.values():
            if sub.user_id != user_id or sub.chat_id != chat_id:
                continue
            if ws in sub.subscribers:
                continue
            sub.subscribers.append(ws)
            attached.append((sub.stream_id, sub.tool_name))
            logger.info(
                f"attach_to_chat({sub.stream_id}): attached loading ws "
                f"(subscribers={len(sub.subscribers)})"
            )
        return attached

    async def replay_retained(self, ws: "WebSocket", stream_id: str) -> None:
        sub = self.subscription_for_stream(stream_id)
        if sub is None or sub.bridged_component_id is None or sub.retained_chunk is None:
            return
        try:
            await self._send_chunk_to_ws(sub, ws, sub.retained_chunk, include_html=True)
        except Exception:
            logger.debug(f"replay_retained failed for {stream_id}", exc_info=True)

    def subscription_for_stream(self, stream_id: str) -> Optional[StreamSubscription]:
        for sub in self._active.values():
            if sub.stream_id == stream_id:
                return sub
        for entries in self._dormant.values():
            for sub in entries.values():
                if sub.stream_id == stream_id:
                    return sub
        return None

    def component_id_for(self, stream_id: str) -> Optional[str]:
        sub = self.subscription_for_stream(stream_id)
        return sub.bridged_component_id if sub is not None else None

    async def handle_agent_chunk(self, msg: Any) -> None:
        request_id = getattr(msg, "request_id", "")
        key = self._request_to_key.get(request_id)
        if key is None:
            logger.debug(
                f"handle_agent_chunk: unknown request_id {request_id} "
                f"(subscription torn down?)"
            )
            return
        sub = self._active.get(key)
        if sub is None:
            logger.debug(
                f"handle_agent_chunk: subscription {key} not in _active"
            )
            return

        msg_error = getattr(msg, "error", None)

        if msg_error is not None:
            error_code = msg_error.get("code", "tool_error") if isinstance(msg_error, dict) else "tool_error"
            error_message = msg_error.get("message", "") if isinstance(msg_error, dict) else str(msg_error)
            await self._handle_error(sub, error_code, error_message)
            return

        if sub.retry_attempt > 0:
            logger.info(
                f"stream {sub.stream_id} → ACTIVE (recovery after "
                f"retry_attempt={sub.retry_attempt})"
            )
            sub.retry_attempt = 0
            sub.next_retry_at = None
            sub.last_error_code = None

        if sub.state in (StreamState.STARTING, StreamState.RECONNECTING):
            sub.state = StreamState.ACTIVE

        # Adds prior high-water so retry/wake seq won't dedup-drop
        effective_seq = int(getattr(msg, "seq", 0) or 0) + sub.seq_offset
        chunk = StreamChunk(
            stream_id=sub.stream_id,
            seq=effective_seq,
            components=list(getattr(msg, "components", []) or []),
            raw=getattr(msg, "raw", None),
            error=None,
            terminal=bool(getattr(msg, "terminal", False)),
        )
        sub.max_seq_seen = max(sub.max_seq_seen, effective_seq)
        sub.last_chunk_at = time.monotonic()

        if chunk.components and sub.bridged_component_id is not None:
            sub.retained_chunk = chunk

        # Last-write-wins: newer chunk overwrites the queued one
        if sub.coalesce_slot is not None:
            sub.dropped_count += 1
        sub.coalesce_slot = chunk

        if not sub.send_in_progress:
            asyncio.create_task(self._drain_and_send(sub))

    async def _handle_error(
        self,
        sub: StreamSubscription,
        error_code: str,
        error_message: str,
    ) -> None:
        cls = classify_error(error_code)
        if cls == "auth":
            await self._fail_subscription(sub, error_code, error_message, retryable=False)
            return
        if cls == "terminal":
            retryable = (error_code == "chunk_too_large")
            await self._fail_subscription(sub, error_code, error_message, retryable=retryable)
            return
        if sub.retry_attempt >= MAX_RETRY_ATTEMPTS:
            await self._fail_subscription(sub, error_code, error_message, retryable=True)
            return
        await self._enter_reconnecting(sub, error_code, error_message)

    async def _enter_reconnecting(
        self, sub: StreamSubscription, error_code: str, error_message: str,
    ) -> None:
        sub.state = StreamState.RECONNECTING
        sub.retry_attempt += 1
        sub.last_error_code = error_code
        backoff_seconds = compute_backoff(sub.retry_attempt)
        sub.next_retry_at = time.monotonic() + backoff_seconds

        if sub.request_id:
            self._request_to_key.pop(sub.request_id, None)
            sub.request_id = None
        sub.coalesce_slot = None

        next_retry_ms = int((time.time() + backoff_seconds) * 1000)
        chunk = StreamChunk(
            stream_id=sub.stream_id,
            seq=sub.max_seq_seen + 1,
            components=[],
            error={
                "code": error_code,
                "message": error_message,
                "phase": "reconnecting",
                "attempt": sub.retry_attempt,
                "next_retry_at_ms": next_retry_ms,
                "retryable": False,
            },
        )
        await self._send_chunk_to_subscribers(sub, chunk)

        loop = asyncio.get_running_loop()
        sub._retry_handle = loop.call_later(
            backoff_seconds,
            lambda: asyncio.create_task(self._retry(sub)),
        )
        logger.info(
            f"stream {sub.stream_id} → RECONNECTING "
            f"(attempt {sub.retry_attempt}/{MAX_RETRY_ATTEMPTS}, "
            f"backoff={backoff_seconds:.1f}s, code={error_code})"
        )

    async def _retry(self, sub: StreamSubscription) -> None:
        if sub.state != StreamState.RECONNECTING:
            return
        if sub.key not in self._active:
            return
        if not sub.subscribers:
            return

        sub.state = StreamState.STARTING
        sub.next_retry_at = None
        sub._retry_handle = None
        sub.seq_offset = sub.max_seq_seen

        if self._agent_dispatcher is None:
            return
        try:
            request_id = await self._agent_dispatcher(
                sub.agent_id, sub.tool_name, sub.params,
                sub.stream_id, sub.user_id,
                sub.subscribers[0], sub.chat_id,
            )
            sub.request_id = request_id
            self._request_to_key[request_id] = sub.key
            logger.info(
                f"stream {sub.stream_id} retry attempt {sub.retry_attempt} "
                f"dispatched as request {request_id}"
            )
        except Exception as e:
            await self._handle_error(sub, "upstream_unavailable", str(e))

    async def _fail_subscription(
        self,
        sub: StreamSubscription,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        if sub._retry_handle is not None:
            sub._retry_handle.cancel()
            sub._retry_handle = None

        chunk = StreamChunk(
            stream_id=sub.stream_id,
            seq=sub.max_seq_seen + 1,
            components=[],
            error={
                "code": error_code,
                "message": error_message,
                "phase": "failed",
                "retryable": retryable,
            },
            terminal=True,
        )
        await self._send_chunk_to_subscribers(sub, chunk)
        await self._cancel_on_agent(sub)
        self._teardown_subscription(sub, StreamState.FAILED, reason=error_code)
        await self._fire_terminal_hook(sub)

    async def handle_agent_end(self, msg: Any) -> None:
        request_id = getattr(msg, "request_id", "")
        key = self._request_to_key.get(request_id)
        if key is None:
            return
        sub = self._active.get(key)
        if sub is None:
            return
        terminal_chunk = StreamChunk(
            stream_id=sub.stream_id,
            seq=sub.max_seq_seen + 1,
            components=[],
            terminal=True,
        )
        await self._send_chunk_to_subscribers(sub, terminal_chunk)
        self._teardown_subscription(sub, StreamState.STOPPED, reason="agent_end")
        await self._fire_terminal_hook(sub)

    async def _drain_and_send(self, sub: StreamSubscription) -> None:
        if sub.send_in_progress:
            return
        sub.send_in_progress = True
        try:
            while sub.coalesce_slot is not None:
                if sub.max_fps > 0:
                    min_interval = 1.0 / sub.max_fps
                    elapsed = time.monotonic() - sub.last_send_at
                    if elapsed < min_interval:
                        await asyncio.sleep(min_interval - elapsed)

                chunk = sub.coalesce_slot
                sub.coalesce_slot = None
                if chunk is None:
                    break

                await self._send_chunk_to_subscribers(sub, chunk)
                sub.last_send_at = time.monotonic()
        except Exception as e:  # pragma: no cover
            logger.error(
                f"_drain_and_send raised for stream {sub.stream_id}: {e}"
            )
        finally:
            sub.send_in_progress = False

    async def _send_chunk_to_subscribers(
        self, sub: StreamSubscription, chunk: StreamChunk,
    ) -> None:
        if not sub.subscribers:
            return

        invalid_subscribers: List[Any] = []
        now = int(time.time())

        for ws in list(sub.subscribers):
            session = self._get_user_session(ws)
            auth_failed_code: Optional[str] = None
            if session is None:
                auth_failed_code = "unauthenticated"
            elif session.get("sub") != sub.user_id:
                auth_failed_code = "unauthorized"
            else:
                expires_at = session.get("expires_at")
                if isinstance(expires_at, (int, float)) and expires_at > 0 and expires_at < now:
                    auth_failed_code = "unauthenticated"

            if auth_failed_code is not None:
                err_chunk = StreamChunk(
                    stream_id=sub.stream_id,
                    seq=sub.delivered_count + 1,
                    components=[],
                    error={
                        "code": auth_failed_code,
                        "message": (
                            "Your session has expired. Please sign in again."
                            if auth_failed_code == "unauthenticated"
                            else "You are no longer authorized for this stream."
                        ),
                        "phase": "failed",
                        "retryable": False,
                    },
                    terminal=True,
                )
                try:
                    await self._send_chunk_to_ws(sub, ws, err_chunk)
                except Exception:
                    pass
                invalid_subscribers.append(ws)
                continue

            try:
                adapted_components = chunk.components
                if chunk.components and self._rote is not None:
                    try:
                        adapted_components = self._rote.adapt(ws, chunk.components)
                    except Exception as e:  # pragma: no cover
                        logger.warning(
                            f"ROTE adapt failed for stream {sub.stream_id}: {e}"
                        )
                        adapted_components = chunk.components

                chunk_html = None
                if adapted_components:
                    try:
                        from webrender import render_for_target, target_for_profile
                        profile = self._rote.get_profile(ws) if self._rote is not None else None
                        chunk_html = render_for_target(target_for_profile(profile), adapted_components, profile)
                    except Exception:
                        logger.exception("webrender: failed to render stream chunk")

                wire_msg = {
                    "type": "ui_stream_data",
                    "stream_id": sub.stream_id,
                    "session_id": sub.chat_id,
                    "seq": chunk.seq,
                    "components": adapted_components,
                    "html": chunk_html,
                    "raw": chunk.raw,
                    "terminal": chunk.terminal,
                    "error": chunk.error,
                }
                if sub.bridged_component_id is not None:
                    wire_msg["component_id"] = sub.bridged_component_id
                await self._send_to_ws(ws, json.dumps(wire_msg))
                sub.delivered_count += 1
            except Exception as e:  # pragma: no cover
                logger.warning(
                    f"send to subscriber failed for stream {sub.stream_id}: {e}"
                )

        for ws in invalid_subscribers:
            if ws in sub.subscribers:
                sub.subscribers.remove(ws)

        if not sub.subscribers and sub.state in (
            StreamState.STARTING, StreamState.ACTIVE, StreamState.RECONNECTING,
        ):
            await self._move_to_dormant(sub, reason="all_subscribers_invalid")

    def _teardown_subscription(
        self,
        sub: StreamSubscription,
        new_state: StreamState,
        reason: str = "",
    ) -> None:
        sub.state = new_state
        sub.state_reason = reason
        if sub._retry_handle is not None:
            sub._retry_handle.cancel()
            sub._retry_handle = None
        if sub.task is not None and not sub.task.done():
            sub.task.cancel()
            sub.task = None
        self._active.pop(sub.key, None)
        if sub.request_id:
            self._request_to_key.pop(sub.request_id, None)
        logger.info(
            f"subscription {sub.stream_id} → {new_state.value} "
            f"(reason={reason})"
        )

    def _ensure_sweeper_running(self) -> None:
        if self._sweep_task is None or self._sweep_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover
                return
            self._sweep_task = loop.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        try:
            while not self._shutdown:
                await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
                if self._shutdown:
                    break
                try:
                    self._sweep_dormant_ttl()
                except Exception as e:  # pragma: no cover
                    logger.error(f"sweep_dormant_ttl raised: {e}")
                try:
                    await self._sweep_token_revocation()
                except Exception as e:  # pragma: no cover
                    logger.error(f"sweep_token_revocation raised: {e}")
        except asyncio.CancelledError:
            pass

    def _sweep_dormant_ttl(self) -> None:
        now = time.monotonic()
        evicted = 0
        for outer_key in list(self._dormant.keys()):
            entries = self._dormant[outer_key]
            for inner_key in list(entries.keys()):
                sub = entries[inner_key]
                if (now - sub.created_at) > DORMANT_TTL_SECONDS:
                    entries.pop(inner_key, None)
                    evicted += 1
                    sub.state = StreamState.STOPPED
                    sub.state_reason = "dormant_ttl"
                    if self.terminal_hook is not None:
                        asyncio.create_task(self._fire_terminal_hook(sub))
            if not entries:
                self._dormant.pop(outer_key, None)
        if evicted > 0:
            logger.info(f"dormant TTL sweep evicted {evicted} entries")

    async def _sweep_token_revocation(self) -> None:
        now = int(time.time())
        for sub in list(self._active.values()):
            needs_check = False
            for ws in list(sub.subscribers):
                session = self._get_user_session(ws)
                if session is None:
                    needs_check = True
                    break
                expires_at = session.get("expires_at")
                if isinstance(expires_at, (int, float)) and expires_at > 0 and expires_at < now:
                    needs_check = True
                    break
            if not needs_check:
                continue
            keep_alive = StreamChunk(
                stream_id=sub.stream_id,
                seq=sub.delivered_count + 1,
                components=[],
            )
            try:
                await self._send_chunk_to_subscribers(sub, keep_alive)
            except Exception as e:  # pragma: no cover
                logger.debug(f"sweep keep-alive failed: {e}")

    @staticmethod
    def _validate_params_size(params: Dict[str, Any]) -> None:
        size = len(json.dumps(params, default=str))
        if size > MAX_PARAMS_BYTES:
            raise ValueError(
                f"params size {size} exceeds {MAX_PARAMS_BYTES} byte cap"
            )

    def _count_active_for_user(self, user_id: str) -> int:
        return sum(1 for key in self._active if key[0] == user_id)

    def _count_dormant_for_user(self, user_id: str) -> int:
        total = 0
        for (uid, _chat), entries in self._dormant.items():
            if uid == user_id:
                total += len(entries)
        return total

    def shutdown(self) -> None:
        self._shutdown = True
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
        for sub in list(self._active.values()):
            if sub.task and not sub.task.done():
                sub.task.cancel()
            if sub._retry_handle is not None:
                sub._retry_handle.cancel()
        self._active.clear()
        self._dormant.clear()
        self._request_to_key.clear()
