"""
StreamManager — owns the lifecycle of every active push-streaming subscription
in the orchestrator process.

This module is the orchestrator-side counterpart to ``backend/shared/stream_sdk.py``
on the agent side. The agent emits ``ToolStreamData`` chunks; the orchestrator
forwards them (after ROTE adaptation and per-subscriber authorization) to every
websocket subscribed to the corresponding ``StreamSubscription``.

Why is this extracted from orchestrator.py?
    [backend/orchestrator/orchestrator.py](orchestrator.py) is already 1800+
    lines. The stream lifecycle is non-trivial (state machine + dormant table
    + retry timer + coalescing buffer + per-subscriber send loop), so it lives
    here for readability. The Orchestrator instantiates a StreamManager and
    wires it into the existing message dispatch.

Design references (read these before editing):
    - specs/001-tool-stream-ui/data-model.md     (entities, state machine)
    - specs/001-tool-stream-ui/research.md       (decisions §1-§12)
    - specs/001-tool-stream-ui/contracts/        (wire protocol)

State machine in one diagram:

    (none) ──subscribe──► STARTING ──first chunk──► ACTIVE
                              │                       │
                              │ tool error            │ load_chat away
                              │ before any chunk      │  OR ws disconnect
                              ▼                       │  OR last subscriber
                          FAILED                      │
                                                      ▼
                            ┌─────► RECONNECTING (1s, 5s, 15s)
                            │           │
                            │           │ 3 attempts exhausted
                            │           ▼
                            │       FAILED
                            │
                            │ first chunk after retry
                            │
                          ACTIVE                  ┌──────────┐
                                                  │ DORMANT  │
                                                  └────┬─────┘
                                                       │ subscriber returns
                                                       ▼
                                                  STARTING (fresh task,
                                                            same stream_id)

    Auth failures (unauthenticated, unauthorized) bypass RECONNECTING entirely
    and go straight ACTIVE → FAILED. (research §12, security carve-out)

Phase 2 of the implementation installs this skeleton with the full data model
and method stubs. Each user story phase fills in one slice of the state
machine: US1 = STARTING/ACTIVE happy path, US2 = ACTIVE→DORMANT, US3 =
DORMANT→STARTING, US4 = multi-subscriber dedup + per-subscriber auth, US5 =
RECONNECTING/auto-retry/auth carve-out.
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


# =============================================================================
# Constants (kept module-level so tests can monkey-patch them)
# =============================================================================

#: Per-user maximum number of distinct ACTIVE/STARTING/RECONNECTING subscriptions.
#: Multi-tab fan-out (FR-009a) does NOT consume additional slots — see
#: data-model.md §3 cardinality. Matches the existing
#: ``Orchestrator._MAX_STREAM_SUBSCRIPTIONS`` for the polling path.
MAX_STREAM_SUBSCRIPTIONS = 10

#: Per-user maximum number of DORMANT subscriptions (across all chats).
#: When this is exceeded, the LRU dormant entry is evicted.
MAX_DORMANT_PER_USER = 50

#: How long a DORMANT subscription survives before the sweeper evicts it.
DORMANT_TTL_SECONDS = 3600  # 1 hour

#: Maximum bytes per stream chunk (overridable per tool via streaming metadata).
DEFAULT_MAX_CHUNK_BYTES = 65536

#: Default per-stream coalescing FPS bounds (overridable per tool).
DEFAULT_MAX_FPS = 30
DEFAULT_MIN_FPS = 5

#: Per-subscription max param size in bytes (rejected at subscribe time).
MAX_PARAMS_BYTES = 16384

#: Background sweeper interval (TTL eviction + token revocation check).
SWEEP_INTERVAL_SECONDS = 60.0

#: Auto-retry backoff intervals (research §12: 1s, 5s, 15s with ±20% jitter).
RETRY_BACKOFF_SECONDS: Tuple[float, ...] = (1.0, 5.0, 15.0)

#: Maximum auto-retry attempts before transitioning to FAILED.
MAX_RETRY_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)


# =============================================================================
# Types
# =============================================================================

class StreamState(enum.Enum):
    """Enumeration of all possible stream subscription states.

    See data-model.md §3 state transitions diagram. Auth failures bypass
    RECONNECTING and go directly ACTIVE → FAILED.
    """
    STARTING = "starting"
    ACTIVE = "active"
    RECONNECTING = "reconnecting"
    DORMANT = "dormant"
    STOPPED = "stopped"
    FAILED = "failed"


#: Active-table key shape: (user_id, chat_id, tool_name, params_hash).
#: This is the FR-009a deduplication key — multiple subscribes from the same
#: user matching this tuple attach to the existing entry instead of creating
#: a new one.
StreamKey = Tuple[str, str, str, str]


@dataclass
class StreamChunk:
    """In-memory representation of a single chunk in flight from agent to
    UI. Mirrors the wire-form ``ToolStreamData``. See data-model.md §5."""
    stream_id: str
    seq: int
    components: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None
    terminal: bool = False


@dataclass
class StreamSubscription:
    """The full subscription record. Lives in either ``StreamManager._active``
    or ``StreamManager._dormant`` (never both). See data-model.md §3.

    Note on ``subscribers``: this list IS the population of websockets that
    will receive each chunk. The list is populated on first subscribe and
    grows as additional tabs of the same user attach (FR-009a). When it
    becomes empty, the subscription transitions to DORMANT.
    """
    stream_id: str
    user_id: str
    chat_id: str
    tool_name: str
    agent_id: str
    params: Dict[str, Any]
    params_hash: str
    # 055 US2: workspace rule-2 fingerprint when FF_STREAM_ARTIFACTS bridged
    # this stream at subscribe; equals stream_id when unbridged (legacy).
    component_id: str
    subscribers: List["WebSocket"] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    last_chunk_at: Optional[float] = None
    state: StreamState = StreamState.STARTING
    state_reason: Optional[str] = None

    # FR-021a: auto-retry state
    retry_attempt: int = 0
    next_retry_at: Optional[float] = None
    last_error_code: Optional[str] = None
    _retry_handle: Optional[asyncio.TimerHandle] = field(default=None, repr=False)

    # In-flight execution state
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    request_id: Optional[str] = None  # Current outstanding agent request_id
    coalesce_slot: Optional[StreamChunk] = field(default=None, repr=False)
    send_in_progress: bool = False
    last_send_at: float = 0.0  # Monotonic time of last successful send (for FPS clamp)

    # Observability
    delivered_count: int = 0
    dropped_count: int = 0

    # 055 US2: newest chunk carrying component content (heartbeats/empty
    # deltas never overwrite it) — the orchestrator's persist-on-terminal
    # payload. Only ever set on bridged subscriptions; never persisted here.
    retained_chunk: Optional[StreamChunk] = field(default=None, repr=False)
    # Clients dedup on monotonic seq keyed by stream_id, but each agent RUN
    # restarts seq near 0 — so retry/dormant-wake re-dispatches must offset
    # inbound seqs above the previous high-water or every recovered frame is
    # silently dropped until the new run catches up.
    max_seq_seen: int = 0
    seq_offset: int = 0
    # Set by the orchestrator's persist-on-terminal so the in-band wrapper
    # and the manager's terminal hook can both fire without double-writing.
    persist_done: bool = False

    # Per-tool metadata snapshot (so we don't have to look it up on every chunk)
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES
    max_fps: int = DEFAULT_MAX_FPS
    min_fps: int = DEFAULT_MIN_FPS

    @property
    def key(self) -> StreamKey:
        return (self.user_id, self.chat_id, self.tool_name, self.params_hash)

    @property
    def bridged_component_id(self) -> Optional[str]:
        """Workspace identity assigned by the 055 bridge, or None when
        unbridged — unbridged frames must stay byte-identical to pre-055."""
        return self.component_id if self.component_id != self.stream_id else None


# =============================================================================
# Helpers
# =============================================================================

def params_hash(params: Dict[str, Any]) -> str:
    """Compute a stable, canonical hash of tool parameters.

    Used to detect duplicate subscribes from the same user (FR-009a) — two
    tabs subscribing to the same tool with the same params yield the same
    hash and attach to the existing subscription.

    Returns the first 16 hex chars of the SHA-256 of the JSON-canonicalised
    params (keys sorted, no whitespace). Pure function, deterministic.
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def compute_backoff(attempt: int) -> float:
    """Return the (jittered) seconds to wait before retry attempt N.

    Attempt is 1-indexed: 1, 2, or 3. Returns the corresponding entry from
    ``RETRY_BACKOFF_SECONDS`` multiplied by ``random.uniform(0.8, 1.2)``
    for ±20% jitter (research §12, mitigates thundering-herd on a recovering
    upstream).
    """
    if not (1 <= attempt <= MAX_RETRY_ATTEMPTS):
        raise ValueError(f"retry attempt out of range: {attempt}")
    base = RETRY_BACKOFF_SECONDS[attempt - 1]
    return base * random.uniform(0.8, 1.2)


#: Error code → routing class. Drives the auto-retry / auth-bypass logic.
#: See data-model.md §6 classification table and research §12.
ErrorClass = str  # "transient" | "auth" | "terminal"

_ERROR_CLASSIFICATION: Dict[str, ErrorClass] = {
    # Transient: auto-retry with backoff
    "tool_error": "transient",
    "upstream_unavailable": "transient",
    "rate_limited": "transient",
    # Auth: bypass retry, surface re-auth UI
    "unauthenticated": "auth",
    "unauthorized": "auth",
    # Terminal: no retry, surface failure
    "chunk_too_large": "terminal",
    "cancelled": "terminal",
}


def classify_error(code: str) -> ErrorClass:
    """Return the routing class for an error code.

    Unknown codes default to "transient" so a tool author who invents a new
    error type doesn't accidentally bypass the retry path. Auth codes are
    explicitly enumerated and never auto-retried (security carve-out).
    """
    return _ERROR_CLASSIFICATION.get(code, "transient")


#: A line-leading list marker ("* ", "- ", "3. ") — its "*" is a bullet, not
#: an emphasis opener, and a boundary before any item content would flash the
#: bare marker as literal text (webrender/sanitize.py _LIST_START).
_LIST_MARKER = re.compile(r"\s*(?:[-*]|\d+\.)[ \t]+")


def _inline_safe_len(line: str) -> int:
    """Last safe whitespace boundary within one (partial) line: the offset
    just past the last whitespace reached with every inline ``**``/``*``/
    backtick/``[label](target`` span closed. Pure function; conservative —
    an ambiguous trailing token is simply held."""
    marker = _LIST_MARKER.match(line)
    content_start = marker.end() if marker else 0
    bold = italic = code = False
    link_depth = 0
    in_target = False        # inside the (...) of [label](target)
    pending_target = False   # "]" just closed the label; "(" opens the target
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
        # No backslash-escape rule: the renderer (webrender/sanitize.py
        # inline_md) has none, and the scanner must mirror what will RENDER —
        # honoring escapes here would ship a "\`" the renderer treats as a
        # live backtick.
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
    """Length of the longest prefix of ``text`` safe to ship as an incremental
    narrative frame (055 research D5, spec FR-013).

    Safe = ends at a whitespace boundary outside every unclosed ``**``/``*``/
    backtick (incl. ``` fence)/``[link(`` span, so no frame ever renders a
    dangling markup token. A completed line outside a fence is always safe:
    the renderer applies inline spans per line (webrender/sanitize.py), so a
    finished line's rendering can never change as text is appended. Returns 0
    while no boundary exists yet — the caller flushes the held tail on the
    terminal chunk. Monotonic under appended text: a boundary once safe stays
    safe (frames are cumulative and must never shrink).
    """
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


# =============================================================================
# StreamManager
# =============================================================================

#: Type alias for the per-ws send function the orchestrator provides.
#: Matches ``Orchestrator._safe_send(websocket, message_str_or_dict)``.
SendFn = Callable[["WebSocket", Any], Awaitable[None]]

#: Type alias for the per-ws session lookup the orchestrator provides.
#: Returns the dict from ``Orchestrator.ui_sessions[ws]``, or None.
GetSessionFn = Callable[["WebSocket"], Optional[Dict[str, Any]]]

#: Type alias for the agent-side dispatcher the orchestrator provides.
#: Sends an ``MCPRequest`` with ``_stream=True`` and ``_stream_id=<sid>`` to
#: the named agent. Does NOT wait for a response (the response stream
#: arrives as ``ToolStreamData`` messages routed back via
#: ``handle_agent_chunk``). Returns the allocated ``request_id`` so the
#: stream manager can populate ``_request_to_key``. Raises if the agent is
#: not connected.
AgentDispatchFn = Callable[
    [
        str,
        str,
        Dict[str, Any],
        str,
        Optional[str],
        "WebSocket",
        str,
    ],  # agent_id, tool_name, args, stream_id, user_id, ws, chat_id
    Awaitable[str],  # returns request_id
]

#: Sends a ``ToolStreamCancel`` to the agent for an in-flight stream.
AgentCancelFn = Callable[[str, str, str], Awaitable[None]]  # agent_id, request_id, stream_id


class StreamManager:
    """Owner of every active and dormant push-streaming subscription.

    Constructed by ``Orchestrator.__init__`` with three callables that wire
    it into the existing infrastructure:

    - ``rote``: the existing :class:`backend.rote.rote.Rote` middleware
      instance, used to adapt outbound chunks per device profile.
    - ``send_to_ws``: the existing ``Orchestrator._safe_send`` helper, used
      to deliver each chunk to a specific websocket.
    - ``get_user_session``: callable returning ``ui_sessions[ws]`` so the
      stream manager can validate per-subscriber auth on every chunk.

    The manager is purely an in-memory facility — no persistent storage.
    Surviving an orchestrator restart is NOT a goal; the frontend's reconnect
    path will re-subscribe naturally (research §3, A-007).
    """

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
        # Set by the orchestrator post-construction (avoids a circular import).
        # Stored as plain callables so tests can substitute mocks.
        self._agent_dispatcher = agent_dispatcher
        self._agent_canceller = agent_canceller
        # Chat ownership check: (ws, user_id, chat_id) -> bool. Defaults to
        # True so unit tests don't need to wire the history DB. The
        # orchestrator passes a real validator that calls history.get_chat.
        self._validate_chat_ownership = validate_chat_ownership

        # Active subscriptions: keyed by (user_id, chat_id, tool_name, params_hash)
        self._active: Dict[StreamKey, StreamSubscription] = {}
        # Dormant subscriptions: outer key (user_id, chat_id), inner key params_hash
        # (matches the dedup key without tool_name+chat_id duplication).
        # Inner value is the same StreamSubscription with state=DORMANT, subscribers=[].
        self._dormant: Dict[Tuple[str, str], Dict[str, StreamSubscription]] = {}
        # Reverse lookup: agent request_id → StreamKey, for routing inbound chunks
        # back to the originating subscription. Populated when we dispatch the
        # MCPRequest to the agent; cleared on subscription teardown.
        self._request_to_key: Dict[str, StreamKey] = {}

        # Background sweeper task (TTL eviction + token revocation check).
        # Started lazily by the first subscribe so unit tests don't have to
        # spin it up.
        self._sweep_task: Optional[asyncio.Task] = None
        self._shutdown = False

        # 055 US2 (FR-011): awaited with the subscription at EVERY terminal
        # transition — including the out-of-band ones no agent frame reaches
        # (retry exhaustion, agent_end while dormant, dormant TTL eviction) —
        # so the orchestrator's persist-on-terminal misses none. The manager
        # itself stays storage-free.
        self.terminal_hook: Optional[Callable[[StreamSubscription], Awaitable[None]]] = None

    async def _fire_terminal_hook(self, sub: StreamSubscription) -> None:
        """Invoke the orchestrator's terminal hook, never letting it break
        stream teardown (persistence is fail-open by design)."""
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

    # ------------------------------------------------------------------
    # Lifecycle: subscribe / attach / unsubscribe / detach / resume
    # ------------------------------------------------------------------

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
        """Subscribe ``ws`` to a streaming tool. Returns ``(stream_id, attached)``.

        ``attached == True`` indicates this websocket joined an EXISTING
        subscription (FR-009a deduplication) instead of creating a new one.
        Attach does NOT count against the per-user concurrency cap.

        US1 (T028) implements the simple "first subscribe" path. US4 (T062)
        adds the dedup-on-match attach path. US3 (T055) adds the
        dormant-wake-on-attach branch.

        Raises:
            ValueError: validation failure. The orchestrator translates these
                into ``stream_error`` replies with the matching code from
                contracts/protocol-messages.md §A6.
        """
        # --- Validation ---------------------------------------------------
        # 1. Verify the websocket is still authenticated as this user
        session = self._get_user_session(ws)
        if session is None:
            raise ValueError("websocket has no active session")
        if session.get("sub") != user_id:
            # Defense in depth — the orchestrator should have already passed
            # the right user_id but we re-check before mutating state.
            raise ValueError("user_id does not match the websocket's session")

        # 2. Chat ownership
        if self._validate_chat_ownership is not None:
            if not await self._validate_chat_ownership(ws, user_id, chat_id):
                raise ValueError(f"chat {chat_id!r} is not owned by this user")

        # 3. Param size cap
        self._validate_params_size(params)

        # 4. Per-user concurrency cap (counts unique subscription keys, not
        #    websockets — multi-tab attaches don't consume slots per FR-009a)
        ph = params_hash(params)
        key: StreamKey = (user_id, chat_id, tool_name, ph)
        if key not in self._active and self._count_active_for_user(user_id) >= MAX_STREAM_SUBSCRIPTIONS:
            raise ValueError(
                f"per-user stream limit ({MAX_STREAM_SUBSCRIPTIONS}) exceeded"
            )

        # --- US4 (T062): dedup-on-match attach path -----------------------
        # If an active subscription with this exact key already exists,
        # attach the requesting websocket instead of allocating a new one.
        # FR-009a: deduplicates by (user_id, chat_id, tool_name, params_hash);
        # multi-tab attach does NOT consume an additional cap slot (the cap
        # check above already exempted matching keys).
        existing = self._active.get(key)
        if existing is not None:
            if ws not in existing.subscribers:
                existing.subscribers.append(ws)
                logger.info(
                    f"subscribe({existing.stream_id}): attached additional "
                    f"ws (subscribers={len(existing.subscribers)})"
                )
                # 055 late-join: the attaching socket missed every prior
                # frame — replay the retained chunk so it renders current
                # state, not a blank placeholder. Bridged-only (retention is
                # bridged-only), so unbridged attach stays send-free,
                # byte-identical to pre-055.
                await self.replay_retained(ws, existing.stream_id)
            return existing.stream_id, True

        # --- US4 (T063): dormant-wake attach path -------------------------
        # If a dormant subscription with this exact key exists, wake it up
        # (mirrors the resume() path but triggered by a fresh subscribe
        # rather than load_chat). The user gets fresh data; the SAME
        # stream_id is preserved.
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

        # --- Allocate fresh subscription ----------------------------------
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

        # Lazily start the background sweeper now that we have at least one
        # active subscription.
        self._ensure_sweeper_running()

        # --- Dispatch the streaming request to the agent ------------------
        # The agent runs the tool as an async generator and emits
        # ToolStreamData chunks back via handle_agent_message →
        # handle_agent_chunk on this side.
        if self._agent_dispatcher is None:
            # Tests run without a real dispatcher; the subscription is
            # registered but no chunks will arrive. The handle_agent_chunk
            # method is callable directly in tests for verification.
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
            # Dispatch failed (agent disconnected, etc.). Tear down the
            # subscription we just registered and surface as
            # agent_unavailable.
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
        """Remove ``ws`` from a subscription's subscribers list (US4 T066).

        Per FR-009a, only when the list becomes empty does the stream
        transition to STOPPED (explicit teardown). Other subscribers — other
        tabs of the same user — keep receiving chunks.

        US2 phase: minimal implementation (single-subscriber assumption).
        US4 will add the multi-tab semantics.
        """
        # Find the subscription by stream_id
        target_sub = None
        for sub in list(self._active.values()):
            if sub.stream_id == stream_id:
                target_sub = sub
                break
        if target_sub is None:
            raise ValueError(f"unknown stream_id {stream_id!r}")

        # Defense in depth: caller must own the stream
        session = self._get_user_session(ws)
        if session is None or session.get("sub") != target_sub.user_id:
            raise ValueError("not authorized to unsubscribe this stream")

        if ws in target_sub.subscribers:
            target_sub.subscribers.remove(ws)

        # If no subscribers remain → tear down (STOPPED, not DORMANT,
        # because explicit unsubscribe means the user won't be coming back).
        if not target_sub.subscribers:
            await self._cancel_on_agent(target_sub)
            # Send terminal chunk to the requesting ws (unsubscribe ack)
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
            # An indefinite stream's only success-terminal is user-initiated:
            # what streamed is what persists (FR-011); deleting the persisted
            # card is a separate, explicit workspace verb.
            await self._fire_terminal_hook(target_sub)

    async def detach(self, ws: "WebSocket") -> None:
        """Called by the orchestrator on WebSocket disconnect (US2 T040).

        Iterates every active subscription, removes ``ws`` from each
        subscribers list, and transitions any subscription whose list becomes
        empty to DORMANT (NOT stopped — the user might come back to the chat
        from a fresh tab).
        """
        for sub in list(self._active.values()):
            if ws in sub.subscribers:
                sub.subscribers.remove(ws)
                if not sub.subscribers:
                    await self._move_to_dormant(sub, reason="ws_disconnect")

    async def pause_chat(self, ws: "WebSocket", old_chat_id: str) -> None:
        """Variant of ``detach`` scoped to one chat (US2 T041).

        Removes ``ws`` only from subscriptions whose ``chat_id == old_chat_id``.
        Used by the orchestrator's ``load_chat`` handler when the user
        switches to a different chat in the same websocket.
        """
        for sub in list(self._active.values()):
            if sub.chat_id != old_chat_id:
                continue
            if ws in sub.subscribers:
                sub.subscribers.remove(ws)
                if not sub.subscribers:
                    await self._move_to_dormant(sub, reason="load_chat_away")

    async def _move_to_dormant(self, sub: StreamSubscription, reason: str) -> None:
        """Transition an ACTIVE / RECONNECTING subscription to DORMANT.

        Cancels the agent-side stream (sends ToolStreamCancel), cancels any
        pending retry timer, moves the record from ``_active`` to
        ``_dormant[(user_id, chat_id)][params_hash]``. Enforces the per-user
        dormant cap via LRU eviction (T045).
        """
        await self._cancel_on_agent(sub)

        # Clean up retry state if we were mid-RECONNECTING
        if sub._retry_handle is not None:
            sub._retry_handle.cancel()
            sub._retry_handle = None
        sub.retry_attempt = 0
        sub.next_retry_at = None
        sub.task = None
        sub.coalesce_slot = None
        sub.send_in_progress = False

        # Move from active → dormant
        self._active.pop(sub.key, None)
        if sub.request_id:
            self._request_to_key.pop(sub.request_id, None)
            sub.request_id = None
        sub.state = StreamState.DORMANT
        sub.state_reason = reason

        # LRU eviction if per-user dormant cap exceeded
        if self._count_dormant_for_user(sub.user_id) >= MAX_DORMANT_PER_USER:
            self._evict_oldest_dormant_for_user(sub.user_id)

        # Park in the dormant table keyed by (user_id, chat_id) → params_hash
        chat_dict = self._dormant.setdefault((sub.user_id, sub.chat_id), {})
        chat_dict[sub.params_hash] = sub
        # Refresh created_at so the TTL clock starts at the moment we became
        # dormant (not at the original subscribe time).
        sub.created_at = time.monotonic()
        logger.info(
            f"subscription {sub.stream_id} → DORMANT "
            f"(reason={reason}, user={sub.user_id}, chat={sub.chat_id})"
        )

    async def _cancel_on_agent(self, sub: StreamSubscription) -> None:
        """Send ToolStreamCancel to the agent for this subscription's
        in-flight request, if a canceller and a request_id are available.
        Errors are swallowed (the agent may already be gone).
        """
        if self._agent_canceller is None or not sub.request_id:
            return
        try:
            await self._agent_canceller(sub.agent_id, sub.request_id, sub.stream_id)
        except Exception as e:
            logger.debug(
                f"_cancel_on_agent failed for {sub.stream_id}: {e}"
            )

    def _evict_oldest_dormant_for_user(self, user_id: str) -> None:
        """LRU eviction: drop the oldest dormant entry for a user when the
        per-user cap is exceeded (T045). 'Oldest' is by ``created_at``.
        """
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
        """Send a single chunk to a single ws (used by unsubscribe ack and
        per-subscriber error chunks in US4). ``include_html`` server-renders
        the components exactly like the fan-out path — the late-join replay
        needs it so web clients (which render from ``html``) can paint the
        frame; acks/error chunks omit the key so their frames stay
        byte-identical to pre-055."""
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
        """Re-activate any DORMANT subscriptions for ``(user_id, chat_id)``.

        US3 T053 implementation. For each dormant entry that matches:

        - Pop from the dormant table.
        - Append ``ws`` to a fresh ``subscribers`` list.
        - Dispatch a fresh agent task with the SAME ``stream_id`` and SAME
          ``params`` so the frontend's existing component (keyed by
          ``stream_id``) merges the next chunk in place.
        - Reset retry state.

        On dispatch failure (e.g. agent gone), transition the subscription
        to FAILED with ``error.code = "upstream_unavailable"`` and send a
        single error chunk to the requesting ws so the frontend can render
        the manual retry button (US3 acceptance scenario 3).

        Returns a list of ``(stream_id, tool_name)`` tuples for the resumed
        subscriptions so the orchestrator can send corresponding
        ``stream_subscribed`` confirmations to the client.
        """
        outer_key = (user_id, chat_id)
        dormant_dict = self._dormant.get(outer_key)
        if not dormant_dict:
            return []

        # Verify ws belongs to this user
        session = self._get_user_session(ws)
        if session is None or session.get("sub") != user_id:
            logger.warning(
                f"resume called with mismatched user/session for {user_id}"
            )
            return []

        resumed: List[Tuple[str, str]] = []
        # Iterate a copy because we mutate the dict
        for params_hash_key, sub in list(dormant_dict.items()):
            try:
                # Pop from dormant
                dormant_dict.pop(params_hash_key, None)
                # Reset state for a fresh STARTING
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
                # The woken agent run restarts seq near 0, but the client that
                # watched the pre-dormant run still holds the old high-water.
                sub.seq_offset = sub.max_seq_seen

                # Re-register in active
                self._active[sub.key] = sub

                # Dispatch fresh agent request — keep the SAME stream_id so
                # the frontend's existing component merges the next chunk
                # in place. Without a dispatcher (test mode), skip dispatch
                # and rely on subsequent handle_agent_chunk calls.
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
                        # Tool no longer available — surface as a failure
                        # chunk with retryable=True so the user can manually
                        # retry. (US3 acceptance scenario 3 / T056.)
                        logger.warning(
                            f"resume dispatch failed for {sub.stream_id}: {e}"
                        )
                        # Transition to FAILED + send error chunk
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

        # Clean up the empty outer_key entry
        if not dormant_dict:
            self._dormant.pop(outer_key, None)

        return resumed

    async def attach_to_chat(
        self, ws: "WebSocket", user_id: str, chat_id: str,
    ) -> List[Tuple[str, str]]:
        """Attach ``ws`` to every ACTIVE subscription of ``(user_id, chat_id)``.

        055 late-join: ``resume()`` only covers DORMANT streams, so a socket
        loading a chat whose streams are kept ACTIVE by the user's other
        sockets would otherwise never receive chunks. Attach-only — the agent
        run is already live, nothing is re-dispatched. Returns
        ``(stream_id, tool_name)`` for each newly attached subscription so
        the orchestrator can send the matching ``stream_subscribed`` acks
        (mirrors ``resume()``).
        """
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
        """Re-deliver the retained content chunk to ONE just-attached socket
        so it shows the stream's current state instead of a blank placeholder
        (055 late-join). Bridged-only by construction — retention itself is
        bridged-only. The chunk keeps its stored seq: the joining socket has
        no seq state for this stream so it accepts, while an already-caught-up
        socket's dedup drops the duplicate. Fail-open: a lost replay degrades
        to waiting for the next live chunk.
        """
        sub = self.subscription_for_stream(stream_id)
        if sub is None or sub.bridged_component_id is None or sub.retained_chunk is None:
            return
        try:
            await self._send_chunk_to_ws(sub, ws, sub.retained_chunk, include_html=True)
        except Exception:
            logger.debug(f"replay_retained failed for {stream_id}", exc_info=True)

    # ------------------------------------------------------------------
    # 055 US2: lookups for the orchestrator's stream_subscribed builders
    # and persist-on-terminal wrapper
    # ------------------------------------------------------------------

    def subscription_for_stream(self, stream_id: str) -> Optional[StreamSubscription]:
        """Look up a subscription by stream_id — active first, then dormant."""
        for sub in self._active.values():
            if sub.stream_id == stream_id:
                return sub
        for entries in self._dormant.values():
            for sub in entries.values():
                if sub.stream_id == stream_id:
                    return sub
        return None

    def component_id_for(self, stream_id: str) -> Optional[str]:
        """Bridged workspace identity for a stream, or None when unbridged
        (flag off) or unknown — ``stream_subscribed`` carries it only when
        non-None so legacy acks stay byte-identical."""
        sub = self.subscription_for_stream(stream_id)
        return sub.bridged_component_id if sub is not None else None

    # ------------------------------------------------------------------
    # Inbound from agent
    # ------------------------------------------------------------------

    async def handle_agent_chunk(self, msg: Any) -> None:
        """Receive a ``ToolStreamData`` from an agent.

        Routing: look up the subscription via the agent-supplied
        ``request_id``. If the subscription is gone (already torn down), drop
        the chunk silently. Otherwise:

        - On the first chunk, transition STARTING → ACTIVE (US1).
        - If we were RECONNECTING, the first successful chunk transitions
          back to ACTIVE and resets retry_attempt (US5 T077).
        - If ``msg.error`` is set, route via ``_handle_error`` (US5 T078).
          Auth codes go directly to FAILED (security carve-out, research §12);
          transient codes enter the RECONNECTING backoff loop.
        - Otherwise place the chunk into ``coalesce_slot`` (overwriting any
          previous chunk that hasn't been sent yet — last-write-wins per
          research §7) and kick the per-stream send loop.
        """
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

        # US5 T078: agent emitted an error chunk. Route via the central
        # error router (transient → RECONNECTING, auth → FAILED).
        if msg_error is not None:
            error_code = msg_error.get("code", "tool_error") if isinstance(msg_error, dict) else "tool_error"
            error_message = msg_error.get("message", "") if isinstance(msg_error, dict) else str(msg_error)
            await self._handle_error(sub, error_code, error_message)
            return

        # US5 T077: first successful chunk after a retry resets the retry
        # state. We check retry_attempt > 0 (NOT state == RECONNECTING)
        # because by the time the chunk arrives, _retry has already moved
        # the state to STARTING for the fresh dispatch.
        if sub.retry_attempt > 0:
            logger.info(
                f"stream {sub.stream_id} → ACTIVE (recovery after "
                f"retry_attempt={sub.retry_attempt})"
            )
            sub.retry_attempt = 0
            sub.next_retry_at = None
            sub.last_error_code = None

        # State transition: STARTING → ACTIVE on first chunk (also covers
        # the post-retry STARTING → ACTIVE case).
        if sub.state in (StreamState.STARTING, StreamState.RECONNECTING):
            sub.state = StreamState.ACTIVE

        # Wrap the inbound message into our internal StreamChunk shape so the
        # send loop has a stable type. seq is offset past the previous run's
        # high-water after a retry/wake re-dispatch (see StreamSubscription).
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

        # Last-write-wins: any previously-queued chunk is overwritten.
        if sub.coalesce_slot is not None:
            sub.dropped_count += 1
        sub.coalesce_slot = chunk

        # Kick the send loop if not already in flight.
        if not sub.send_in_progress:
            asyncio.create_task(self._drain_and_send(sub))

    # ------------------------------------------------------------------
    # US5: error router + auto-retry (T072–T080)
    # ------------------------------------------------------------------

    async def _handle_error(
        self,
        sub: StreamSubscription,
        error_code: str,
        error_message: str,
    ) -> None:
        """Central error router. See research §12 for the security carve-out.

        - ``transient`` codes enter ``RECONNECTING`` (or terminate as FAILED
          if 3 attempts have already been exhausted).
        - ``auth`` codes (``unauthenticated``/``unauthorized``) bypass the
          retry loop entirely and go directly to FAILED. The frontend shows
          a re-authentication state.
        - ``terminal`` codes (``chunk_too_large``/``cancelled``) go directly
          to FAILED with no retry.
        """
        cls = classify_error(error_code)
        if cls == "auth":
            await self._fail_subscription(sub, error_code, error_message, retryable=False)
            return
        if cls == "terminal":
            # chunk_too_large is recoverable by user fix → retryable=True
            # cancelled is not (the user explicitly stopped) → retryable=False
            retryable = (error_code == "chunk_too_large")
            await self._fail_subscription(sub, error_code, error_message, retryable=retryable)
            return
        # Transient: enter RECONNECTING if we have retries left, else FAIL
        if sub.retry_attempt >= MAX_RETRY_ATTEMPTS:
            await self._fail_subscription(sub, error_code, error_message, retryable=True)
            return
        await self._enter_reconnecting(sub, error_code, error_message)

    async def _enter_reconnecting(
        self, sub: StreamSubscription, error_code: str, error_message: str,
    ) -> None:
        """Transition ACTIVE → RECONNECTING. Sends a reconnecting chunk to
        all subscribers and schedules the next retry attempt.
        """
        sub.state = StreamState.RECONNECTING
        sub.retry_attempt += 1
        sub.last_error_code = error_code
        backoff_seconds = compute_backoff(sub.retry_attempt)
        sub.next_retry_at = time.monotonic() + backoff_seconds

        # Cancel any in-flight task tracking
        if sub.request_id:
            self._request_to_key.pop(sub.request_id, None)
            sub.request_id = None
        sub.coalesce_slot = None

        # Fan out a reconnecting chunk to all subscribers
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

        # Schedule the retry callback
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
        """Backoff timer fired — re-issue the agent dispatch with the same
        stream_id and same params. If subscribe fails, route through
        ``_handle_error`` again so we may either retry once more or give up.
        """
        # Bail if state changed (e.g. user left and we went DORMANT)
        if sub.state != StreamState.RECONNECTING:
            return
        if sub.key not in self._active:
            return
        if not sub.subscribers:
            return

        sub.state = StreamState.STARTING
        sub.next_retry_at = None
        sub._retry_handle = None
        # The fresh agent run restarts seq near 0 — continue above the old
        # high-water so client dedup (keyed on stream_id) keeps accepting.
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
        """Transition to FAILED, send a final error chunk to all subscribers,
        and tear down."""
        # Cancel any pending retry
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
        # Cancel the agent-side stream if still tracked
        await self._cancel_on_agent(sub)
        self._teardown_subscription(sub, StreamState.FAILED, reason=error_code)
        await self._fire_terminal_hook(sub)

    async def handle_agent_end(self, msg: Any) -> None:
        """Receive a ``ToolStreamEnd`` from an agent. Mark the subscription
        terminal, send a final ``ui_stream_data`` chunk with ``terminal=true``,
        clean up.

        US1 (T031) full implementation.
        """
        request_id = getattr(msg, "request_id", "")
        key = self._request_to_key.get(request_id)
        if key is None:
            # Unroutable by design for DORMANT/RECONNECTING streams — both
            # transitions pop the request mapping (and dormancy cancels the
            # agent run); abandoned retained content is persisted by the
            # dormant-TTL terminal hook instead.
            return
        sub = self._active.get(key)
        if sub is None:
            return
        # Send a terminal chunk to all subscribers so the frontend knows the
        # stream is done.
        terminal_chunk = StreamChunk(
            stream_id=sub.stream_id,
            seq=sub.max_seq_seen + 1,
            components=[],
            terminal=True,
        )
        # Bypass coalescing for the terminal — send immediately to all subs.
        await self._send_chunk_to_subscribers(sub, terminal_chunk)
        # Clean up
        self._teardown_subscription(sub, StreamState.STOPPED, reason="agent_end")
        await self._fire_terminal_hook(sub)

    # ------------------------------------------------------------------
    # Send loop (US1)
    # ------------------------------------------------------------------

    async def _drain_and_send(self, sub: StreamSubscription) -> None:
        """Drain ``sub.coalesce_slot`` and send to all subscribers.

        After sending, if a newer chunk has arrived during the send (via
        another ``handle_agent_chunk`` call placing it in the slot), loops
        again. Honors the ``max_fps`` minimum interval between sends so a
        runaway tool can't saturate the wire.

        US1 implementation: single-subscriber happy path with no auth
        invariant. US4 T065 adds the per-subscriber validation loop.
        """
        if sub.send_in_progress:
            return  # another task is already draining
        sub.send_in_progress = True
        try:
            while sub.coalesce_slot is not None:
                # Honor MAX_FPS minimum interval. We sleep BEFORE the next
                # send so the most recent chunk wins (any chunks arriving
                # during sleep just overwrite the slot).
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
        """Build the ui_stream_data wire message, ROTE-adapt per subscriber,
        and send to each ws in ``sub.subscribers``.

        Per-subscriber authorization invariant (US4 T065 / data-model.md §8):
        before sending to each ws, validate

        1. ws still has an active session
        2. session.sub == sub.user_id (cross-user defense in depth)
        3. session token has not expired

        On any failure, the ws is removed from ``sub.subscribers`` and a
        single ``unauthenticated`` error chunk is sent to JUST that ws.
        Other subscribers continue receiving normal chunks. If the list
        becomes empty as a result, the subscription transitions to DORMANT.

        Cross-user attach is impossible by construction (subscribe always
        appends to a key whose first element is user_id), but the runtime
        check here is the load-bearing security carve-out tested by
        test_stream_isolation.py.
        """
        if not sub.subscribers:
            return

        invalid_subscribers: List[Any] = []
        now = int(time.time())

        for ws in list(sub.subscribers):
            # Per-subscriber authorization invariant (data-model.md §8)
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
                # Send a single failed chunk to just this ws
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
                # ROTE adaptation per device profile (cached per ws by ROTE).
                adapted_components = chunk.components
                if chunk.components and self._rote is not None:
                    try:
                        adapted_components = self._rote.adapt(ws, chunk.components)
                    except Exception as e:  # pragma: no cover
                        logger.warning(
                            f"ROTE adapt failed for stream {sub.stream_id}: {e}"
                        )
                        adapted_components = chunk.components

                # Feature 026: server-render the adapted chunk to HTML for web
                # clients. Structured components stay on the wire (FR-018).
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

        # Remove subscribers whose authorization invariant failed
        for ws in invalid_subscribers:
            if ws in sub.subscribers:
                sub.subscribers.remove(ws)

        # If all subscribers were removed, transition to DORMANT (the user
        # may come back via a fresh tab — same as the leave path).
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
        """Remove a subscription from _active and _request_to_key.
        Cancels any pending retry handle. Used by handle_agent_end and by
        the various unsubscribe paths in later stories.
        """
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

    # ------------------------------------------------------------------
    # Internal helpers (used by story-phase implementations)
    # ------------------------------------------------------------------

    def _ensure_sweeper_running(self) -> None:
        """Lazily start the background sweeper task on first subscribe."""
        if self._sweep_task is None or self._sweep_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover
                return
            self._sweep_task = loop.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        """Background loop: every SWEEP_INTERVAL_SECONDS, evict expired
        DORMANT entries (US2 T044) and check for revoked tokens (US5 T081)."""
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
        """Evict DORMANT entries older than DORMANT_TTL_SECONDS (US2 T044).

        Iterates the dormant table; for each entry whose ``created_at`` (set
        to monotonic time at the moment it became dormant) is older than the
        TTL threshold, drop it. The user is not notified — they aren't
        viewing the chat. On their return, the stream is simply absent and
        they may re-trigger it via the original chat action.
        """
        now = time.monotonic()
        evicted = 0
        for outer_key in list(self._dormant.keys()):
            entries = self._dormant[outer_key]
            for inner_key in list(entries.keys()):
                sub = entries[inner_key]
                if (now - sub.created_at) > DORMANT_TTL_SECONDS:
                    entries.pop(inner_key, None)
                    evicted += 1
                    # Abandonment is a terminal state too (055 FR-011): give
                    # the persist hook its shot at the retained content before
                    # the record is dropped. Sweep is sync — schedule it.
                    sub.state = StreamState.STOPPED
                    sub.state_reason = "dormant_ttl"
                    if self.terminal_hook is not None:
                        asyncio.create_task(self._fire_terminal_hook(sub))
            if not entries:
                self._dormant.pop(outer_key, None)
        if evicted > 0:
            logger.info(f"dormant TTL sweep evicted {evicted} entries")

    async def _sweep_token_revocation(self) -> None:
        """Check every active subscription's subscribers for expired tokens
        (US5 T081, satisfies SC-009).

        For each subscription's subscribers, look up the user session via
        ``_get_user_session(ws)``. If the session is missing OR the
        ``expires_at`` is in the past, route through the per-subscriber
        invalid path of ``_send_chunk_to_subscribers`` by emitting an empty
        keep-alive chunk — the same authorization invariant code path will
        catch the expiry, send the unauthenticated chunk to that ws, and
        remove it from the subscribers list. If the list becomes empty as a
        result, the subscription transitions to DORMANT.

        This satisfies SC-009: revocation latency ≤ ``SWEEP_INTERVAL_SECONDS``.
        """
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
            # Trigger the per-subscriber auth invariant check by sending a
            # no-op chunk through the normal send path. The invariant code
            # in _send_chunk_to_subscribers will detect the expired sessions
            # and emit unauthenticated error chunks to just those ws.
            keep_alive = StreamChunk(
                stream_id=sub.stream_id,
                seq=sub.delivered_count + 1,
                components=[],
            )
            try:
                await self._send_chunk_to_subscribers(sub, keep_alive)
            except Exception as e:  # pragma: no cover
                logger.debug(f"sweep keep-alive failed: {e}")

    # ------------------------------------------------------------------
    # Validation helpers (called from subscribe)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_params_size(params: Dict[str, Any]) -> None:
        """Reject params over 16 KB. Defense against DoS / log bloat."""
        size = len(json.dumps(params, default=str))
        if size > MAX_PARAMS_BYTES:
            raise ValueError(
                f"params size {size} exceeds {MAX_PARAMS_BYTES} byte cap"
            )

    def _count_active_for_user(self, user_id: str) -> int:
        """Number of distinct ACTIVE/STARTING/RECONNECTING subscription keys
        owned by ``user_id``. Multi-tab fan-out (FR-009a) does NOT inflate
        this count — only distinct keys are counted, not subscriber list
        sizes.
        """
        return sum(1 for key in self._active if key[0] == user_id)

    def _count_dormant_for_user(self, user_id: str) -> int:
        """Number of dormant subscriptions across all chats for ``user_id``."""
        total = 0
        for (uid, _chat), entries in self._dormant.items():
            if uid == user_id:
                total += len(entries)
        return total

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Cancel the sweeper and any in-flight stream tasks. Called from
        ``Orchestrator.shutdown``."""
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
